from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tts_more"


def _git_tracked_paths(root: Path, expected: set[str] | dict[str, object]) -> set[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--",
            *sorted(expected),
        ],
        check=True,
        capture_output=True,
    )
    return set(completed.stdout.decode("utf-8", errors="strict").splitlines())


def _active_cmd_lines(path: Path) -> list[str]:
    active: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line or lowered.startswith("rem ") or lowered.startswith("::"):
            continue
        if lowered in {"@echo off", "setlocal", "setlocal enableextensions"}:
            continue
        active.append(line)
    return active


_POWERSHELL_SEMANTIC_CONTRACT = r"""
$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Assert-Contract {
    param([bool]$Condition, [string]$Message)
    if (!$Condition) { throw $Message }
}

function Parse-ContractAst {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    Assert-Contract ($errors.Count -eq 0) ("PowerShell parse failed: " + (($errors | ForEach-Object Message) -join "; "))
    return $ast
}

function Get-ContractFunction {
    param($Ast, [string]$Name)
    $functions = @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | Where-Object { $_.Name -ceq $Name })
    Assert-Contract ($functions.Count -eq 1) ("expected one function: " + $Name)
    return $functions[0]
}

function Get-ContractAssignments {
    param($Ast, [string]$Variable)
    return @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) | Where-Object {
        $_.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Left.VariablePath.UserPath -ceq $Variable
    })
}

function Get-ContractCommands {
    param($Ast, [string]$Name)
    return @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true) | Where-Object { $_.GetCommandName() -ceq $Name })
}

function Get-ContractParameterArgument {
    param($Command, [string]$Name)
    $elements = @($Command.CommandElements)
    for ($index = 1; $index -lt $elements.Count; $index++) {
        $element = $elements[$index]
        if ($element -is [System.Management.Automation.Language.CommandParameterAst] -and $element.ParameterName -ceq $Name) {
            Assert-Contract ($index + 1 -lt $elements.Count) ("missing argument for parameter: " + $Name)
            return $elements[$index + 1]
        }
    }
    return $null
}

function Get-ContractMemberPath {
    param($Node)
    if ($Node -is [System.Management.Automation.Language.VariableExpressionAst]) {
        return $Node.VariablePath.UserPath
    }
    if ($Node -is [System.Management.Automation.Language.MemberExpressionAst]) {
        $prefix = Get-ContractMemberPath $Node.Expression
        if ([string]::IsNullOrWhiteSpace($prefix)) { return "" }
        return $prefix + "." + [string]$Node.Member.Value
    }
    return ""
}

function Test-ContractContainsMemberPath {
    param($Ast, [string]$Expected)
    foreach ($member in @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.MemberExpressionAst] }, $true))) {
        if ((Get-ContractMemberPath $member) -ceq $Expected) { return $true }
    }
    return $false
}

function Test-ContractVariable {
    param($Node, [string]$Expected)
    return $Node -is [System.Management.Automation.Language.VariableExpressionAst] -and $Node.VariablePath.UserPath -ceq $Expected
}

function Test-ContractString {
    param($Node, [string]$Expected)
    return $Node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and [string]$Node.Value -ceq $Expected
}

function Get-ContractExactExpression {
    param($Ast)
    if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) { return $Ast.Expression }
    if (
        $Ast -is [System.Management.Automation.Language.PipelineAst] -and
        $Ast.PipelineElements.Count -eq 1 -and
        $Ast.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]
    ) {
        return $Ast.PipelineElements[0].Expression
    }
    return $null
}

function Get-ContractExactPipelineCommand {
    param($Ast, [string]$Name)
    if ($Ast -isnot [System.Management.Automation.Language.PipelineAst] -or $Ast.PipelineElements.Count -ne 1) { return $null }
    $command = $Ast.PipelineElements[0]
    if ($command -isnot [System.Management.Automation.Language.CommandAst] -or $command.GetCommandName() -cne $Name) { return $null }
    return $command
}

function Test-ContractExactPipelineVariable {
    param($Ast, [string]$Variable)
    return Test-ContractVariable (Get-ContractExactExpression $Ast) $Variable
}

function Test-ContractExactConvertedMember {
    param($Ast, [string]$TypeName, [string]$MemberPath)
    $pipeline = if ($Ast -is [System.Management.Automation.Language.ParenExpressionAst]) { $Ast.Pipeline } else { $Ast }
    $conversion = Get-ContractExactExpression $pipeline
    return $conversion -is [System.Management.Automation.Language.ConvertExpressionAst] -and
        [string]$conversion.Type.TypeName.FullName -ceq $TypeName -and
        (Get-ContractMemberPath $conversion.Child) -ceq $MemberPath
}

function Test-ContractExactSchemaV2Condition {
    param($Ast)
    $binary = Get-ContractExactExpression $Ast
    return $binary -is [System.Management.Automation.Language.BinaryExpressionAst] -and $binary.Operator.ToString() -match "Eq$" -and
        $binary.Left -is [System.Management.Automation.Language.ConvertExpressionAst] -and
        [string]$binary.Left.Type.TypeName.FullName -ceq "int" -and
        (Get-ContractMemberPath $binary.Left.Child) -ceq "manifest.schema_version" -and
        $binary.Right -is [System.Management.Automation.Language.ConstantExpressionAst] -and $binary.Right.Value -eq 2
}

function Test-ContractExactOperationsResolver {
    param($Ast)
    $command = Get-ContractExactPipelineCommand $Ast "Resolve-PortablePackagePath"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 7 -and
        $elements[1] -is [System.Management.Automation.Language.CommandParameterAst] -and $elements[1].ParameterName -ceq "Root" -and
        (Test-ContractVariable $elements[2] "resolvedRoot") -and
        $elements[3] -is [System.Management.Automation.Language.CommandParameterAst] -and $elements[3].ParameterName -ceq "RelativePath" -and
        (Test-ContractExactConvertedMember $elements[4] "string" "manifest.data.operations") -and
        $elements[5] -is [System.Management.Automation.Language.CommandParameterAst] -and $elements[5].ParameterName -ceq "Label" -and
        (Test-ContractString $elements[6] "data.operations")
}

function Test-ContractExactThrowBody {
    param($Block, [string]$Code)
    if ($Block -isnot [System.Management.Automation.Language.StatementBlockAst] -or $Block.Statements.Count -ne 1) { return $false }
    $command = Get-ContractExactPipelineCommand $Block.Statements[0] "Throw-PortableStartError"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 3 -and (Test-ContractString $elements[1] $Code) -and
        $elements[2] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        ![string]::IsNullOrWhiteSpace([string]$elements[2].Value)
}

function Test-ContractExactJoinPathMemberString {
    param($Ast, [string]$RootMember, [string]$Child)
    $command = Get-ContractExactPipelineCommand $Ast "Join-Path"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 3 -and (Get-ContractMemberPath $elements[1]) -ceq $RootMember -and (Test-ContractString $elements[2] $Child)
}

function Test-ContractExactJoinPathVariableString {
    param($Ast, [string]$RootVariable, [string]$Child)
    $command = Get-ContractExactPipelineCommand $Ast "Join-Path"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 3 -and (Test-ContractVariable $elements[1] $RootVariable) -and (Test-ContractString $elements[2] $Child)
}

function Test-ContractExactVariableInvocation {
    param($Node, [string]$Variable, [string]$Method)
    return $Node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and !$Node.Static -and
        (Test-ContractVariable $Node.Expression $Variable) -and [string]$Node.Member.Value -ceq $Method -and $Node.Arguments.Count -eq 0
}

function Test-ContractExactJoinPathMemberInvocation {
    param($Ast, [string]$RootMember, [string]$ChildVariable, [string]$ChildMethod)
    $command = Get-ContractExactPipelineCommand $Ast "Join-Path"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 3 -and (Get-ContractMemberPath $elements[1]) -ceq $RootMember -and
        (Test-ContractExactVariableInvocation $elements[2] $ChildVariable $ChildMethod)
}

function Test-ContractExactStaticInvocation {
    param($Node, [string]$TypeName, [string]$Method, [int]$ArgumentCount)
    return $Node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $Node.Static -and
        $Node.Expression -is [System.Management.Automation.Language.TypeExpressionAst] -and
        [string]$Node.Expression.TypeName.FullName -ceq $TypeName -and [string]$Node.Member.Value -ceq $Method -and
        $Node.Arguments.Count -eq $ArgumentCount
}

function Test-ContractExactGetFullPathVariable {
    param($Ast, [string]$Variable)
    $invoke = Get-ContractExactExpression $Ast
    return (Test-ContractExactStaticInvocation $invoke "IO.Path" "GetFullPath" 1) -and (Test-ContractVariable $invoke.Arguments[0] $Variable)
}

function Test-ContractExactGetFullPathJoinMemberVariable {
    param($Ast, [string]$RootMember, [string]$ChildVariable)
    $invoke = Get-ContractExactExpression $Ast
    if (!(Test-ContractExactStaticInvocation $invoke "IO.Path" "GetFullPath" 1)) { return $false }
    $argument = $invoke.Arguments[0]
    return $argument -is [System.Management.Automation.Language.ParenExpressionAst] -and
        (Test-ContractExactJoinPathMemberInvocation $argument.Pipeline $RootMember $ChildVariable "ToString")
}

function Test-ContractExactGetFullPathJoinMemberChildVariable {
    param($Ast, [string]$RootMember, [string]$ChildVariable)
    $invoke = Get-ContractExactExpression $Ast
    if (!(Test-ContractExactStaticInvocation $invoke "IO.Path" "GetFullPath" 1)) { return $false }
    $argument = $invoke.Arguments[0]
    if ($argument -isnot [System.Management.Automation.Language.ParenExpressionAst]) { return $false }
    $command = Get-ContractExactPipelineCommand $argument.Pipeline "Join-Path"
    if ($null -eq $command) { return $false }
    $elements = @($command.CommandElements)
    return $elements.Count -eq 3 -and (Get-ContractMemberPath $elements[1]) -ceq $RootMember -and (Test-ContractVariable $elements[2] $ChildVariable)
}

function Test-ContractExactParentEquals {
    param($Node, [string]$OperationVariable, [string]$RootMember)
    if (!(Test-ContractExactStaticInvocation $Node "string" "Equals" 3)) { return $false }
    $parentArgument = $Node.Arguments[0]
    if ($parentArgument -isnot [System.Management.Automation.Language.ParenExpressionAst]) { return $false }
    $split = Get-ContractExactPipelineCommand $parentArgument.Pipeline "Split-Path"
    if ($null -eq $split) { return $false }
    $splitElements = @($split.CommandElements)
    if ($splitElements.Count -ne 3 -or $splitElements[1] -isnot [System.Management.Automation.Language.CommandParameterAst] -or
        $splitElements[1].ParameterName -cne "Parent" -or !(Test-ContractVariable $splitElements[2] $OperationVariable)) { return $false }
    $normalizedRoot = $Node.Arguments[1]
    if (!(Test-ContractExactStaticInvocation $normalizedRoot "IO.Path" "GetFullPath" 1) -or
        (Get-ContractMemberPath $normalizedRoot.Arguments[0]) -cne $RootMember) { return $false }
    $comparison = $Node.Arguments[2]
    return $comparison -is [System.Management.Automation.Language.MemberExpressionAst] -and $comparison.Static -and
        $comparison.Expression -is [System.Management.Automation.Language.TypeExpressionAst] -and
        [string]$comparison.Expression.TypeName.FullName -ceq "StringComparison" -and
        [string]$comparison.Member.Value -ceq "OrdinalIgnoreCase"
}

function Test-ContractExactBoundaryCondition {
    param($Ast, [string]$OperationVariable, [string]$RootMember)
    $binary = Get-ContractExactExpression $Ast
    if ($binary -isnot [System.Management.Automation.Language.BinaryExpressionAst] -or $binary.Operator.ToString() -cne "Or") { return $false }
    if ($binary.Left -isnot [System.Management.Automation.Language.UnaryExpressionAst] -or $binary.Left.TokenKind.ToString() -cne "Exclaim" -or
        $binary.Right -isnot [System.Management.Automation.Language.UnaryExpressionAst] -or $binary.Right.TokenKind.ToString() -cne "Exclaim") { return $false }
    $containment = $binary.Left.Child
    if ($containment -isnot [System.Management.Automation.Language.ParenExpressionAst]) { return $false }
    $pathCheck = Get-ContractExactPipelineCommand $containment.Pipeline "Test-PathWithinRoot"
    if ($null -eq $pathCheck) { return $false }
    $elements = @($pathCheck.CommandElements)
    if ($elements.Count -ne 5 -or $elements[1] -isnot [System.Management.Automation.Language.CommandParameterAst] -or $elements[1].ParameterName -cne "Root" -or
        (Get-ContractMemberPath $elements[2]) -cne $RootMember -or $elements[3] -isnot [System.Management.Automation.Language.CommandParameterAst] -or
        $elements[3].ParameterName -cne "Path" -or !(Test-ContractVariable $elements[4] $OperationVariable)) { return $false }
    return Test-ContractExactParentEquals $binary.Right.Child $OperationVariable $RootMember
}

function Test-ContractExactParentBoundaryCondition {
    param($Ast, [string]$OperationVariable, [string]$RootMember)
    $unary = Get-ContractExactExpression $Ast
    return $unary -is [System.Management.Automation.Language.UnaryExpressionAst] -and $unary.TokenKind.ToString() -ceq "Exclaim" -and
        (Test-ContractExactParentEquals $unary.Child $OperationVariable $RootMember)
}

function Test-ContractExactServiceCondition {
    param($Ast)
    $binary = Get-ContractExactExpression $Ast
    return $binary -is [System.Management.Automation.Language.BinaryExpressionAst] -and $binary.Operator.ToString() -match "Eq$" -and
        (Test-ContractVariable $binary.Left "component") -and (Test-ContractString $binary.Right "tts-more")
}

function Test-ContractExactStatementBlockJoinPath {
    param($Block, [string]$RootVariable, [string]$Child)
    return $Block -is [System.Management.Automation.Language.StatementBlockAst] -and $Block.Statements.Count -eq 1 -and
        (Test-ContractExactJoinPathVariableString $Block.Statements[0] $RootVariable $Child)
}

function Test-ContractJoinPath {
    param($Ast, [string]$RootVariable, [string]$RelativePath)
    foreach ($command in @(Get-ContractCommands $Ast "Join-Path")) {
        $elements = @($command.CommandElements)
        if ($elements.Count -eq 3 -and (Test-ContractVariable $elements[1] $RootVariable) -and (Test-ContractString $elements[2] $RelativePath)) {
            return $true
        }
    }
    return $false
}

function Test-ContractDescendantOf {
    param($Node, $Ancestor)
    $current = $Node
    while ($null -ne $current) {
        if ([object]::ReferenceEquals($current, $Ancestor)) { return $true }
        $current = $current.Parent
    }
    return $false
}

function Test-ContractSchemaV2Ancestor {
    param($Node)
    $tryBody = $Node.Parent
    if ($tryBody -isnot [System.Management.Automation.Language.StatementBlockAst]) { return $false }
    $tryStatement = $tryBody.Parent
    if ($tryStatement -isnot [System.Management.Automation.Language.TryStatementAst] -or ![object]::ReferenceEquals($tryStatement.Body, $tryBody)) { return $false }
    $clauseBody = $tryStatement.Parent
    if ($clauseBody -isnot [System.Management.Automation.Language.StatementBlockAst]) { return $false }
    $ifStatement = $clauseBody.Parent
    if ($ifStatement -isnot [System.Management.Automation.Language.IfStatementAst]) { return $false }
    foreach ($clause in $ifStatement.Clauses) {
        if ((Test-ContractExactSchemaV2Condition $clause.Item1) -and [object]::ReferenceEquals($clause.Item2, $clauseBody)) { return $true }
    }
    return $false
}

function Test-ContractTopLevelAssignment {
    param($Assignment)
    return $Assignment.Parent -is [System.Management.Automation.Language.NamedBlockAst] -and $Assignment.Parent.Parent -is [System.Management.Automation.Language.ScriptBlockAst]
}

function Test-ContractDirectCommandInTry {
    param($Command, $TryStatement)
    if ($TryStatement -isnot [System.Management.Automation.Language.TryStatementAst]) { return $false }
    $current = $Command.Parent
    while ($null -ne $current -and ![object]::ReferenceEquals($current, $TryStatement.Body)) {
        if ($current -is [System.Management.Automation.Language.IfStatementAst] -or
            $current -is [System.Management.Automation.Language.SwitchStatementAst] -or
            $current -is [System.Management.Automation.Language.LoopStatementAst] -or
            $current -is [System.Management.Automation.Language.TrapStatementAst] -or
            $current -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
            $current -is [System.Management.Automation.Language.TryStatementAst]) { return $false }
        $current = $current.Parent
    }
    return [object]::ReferenceEquals($current, $TryStatement.Body)
}

$controller = Parse-ContractAst $env:TTS_MORE_CONTRACT_CONTROLLER
$worker = Parse-ContractAst $env:TTS_MORE_CONTRACT_WORKER
$errorFunction = Get-ContractFunction $controller "Throw-PortableStartError"
$contextFunction = Get-ContractFunction $controller "Get-PackageContext"
$serviceFunction = Get-ContractFunction $controller "Invoke-ServiceStart"
$lockFunction = Get-ContractFunction $controller "Open-PackageOperationLock"
$initializeOperationFunction = Get-ContractFunction $controller "Initialize-Operation"
$waitFunction = Get-ContractFunction $controller "Wait-ForActiveOperation"
$clearFunction = Get-ContractFunction $controller "Clear-StaleActivePointer"
$mainTries = @(
    $controller.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.TryStatementAst] },
        $true
    ) | Where-Object { [object]::ReferenceEquals($_.Parent, $controller.EndBlock) }
)
Assert-Contract ($mainTries.Count -eq 1) "controller must have exactly one direct main try statement"
$mainTry = $mainTries[0]

$errorBody = $errorFunction.Body
$cleanBlockProperty = $errorBody.PSObject.Properties["CleanBlock"]
Assert-Contract ($null -eq $errorBody.DynamicParamBlock -and $null -eq $errorBody.BeginBlock -and
    $null -eq $errorBody.ProcessBlock -and ($null -eq $cleanBlockProperty -or $null -eq $cleanBlockProperty.Value)) "Throw-PortableStartError must not contain bypass named blocks"
$errorParameters = @($errorFunction.Body.ParamBlock.Parameters)
Assert-Contract ($errorParameters.Count -eq 2 -and
    $errorParameters[0].Name.VariablePath.UserPath -ceq "Code" -and $errorParameters[0].StaticType -eq [string] -and
    $errorParameters[1].Name.VariablePath.UserPath -ceq "Message" -and
    $errorParameters[1].StaticType -eq [string]) "Throw-PortableStartError must declare exactly string Code and Message parameters"
$errorStatements = @($errorFunction.Body.EndBlock.Statements)
Assert-Contract (
    $errorStatements.Count -eq 1 -and
    $errorStatements[0] -is [System.Management.Automation.Language.ThrowStatementAst]
) "Throw-PortableStartError must contain one direct throw statement"
$errorThrow = $errorStatements[0]
$errorExpression = Get-ContractExactExpression $errorThrow.Pipeline
Assert-Contract ((Test-ContractExactStaticInvocation $errorExpression "PortableStartException" "new" 2) -and
    (Test-ContractVariable $errorExpression.Arguments[0] "Code") -and
    (Test-ContractVariable $errorExpression.Arguments[1] "Message")) "Throw-PortableStartError must directly throw PortableStartException.new(Code, Message)"

$operationsMatches = @()
$v2OperationsAssignments = @()
foreach ($assignment in @(Get-ContractAssignments $contextFunction.Body "operationsRoot")) {
    if (Test-ContractSchemaV2Ancestor $assignment) { $v2OperationsAssignments += $assignment }
    if ((Test-ContractExactOperationsResolver $assignment.Right) -and (Test-ContractSchemaV2Ancestor $assignment)) { $operationsMatches += $assignment }
}
Assert-Contract ($v2OperationsAssignments.Count -eq 1) ("schema-v2 branch must assign operationsRoot exactly once; found " + $v2OperationsAssignments.Count)
Assert-Contract ($operationsMatches.Count -eq 1) "schema-v2 data.operations is not one exact direct resolver assignment"

$contextReturns = @($contextFunction.Body.FindAll({ param($node) $node -is [System.Management.Automation.Language.ReturnStatementAst] }, $true))
$directContextReturns = @($contextReturns | Where-Object { [object]::ReferenceEquals($_.Parent, $contextFunction.Body.EndBlock) })
Assert-Contract ($contextReturns.Count -eq 1 -and $directContextReturns.Count -eq 1) "Get-PackageContext must contain exactly one direct return"
$contextReturn = $directContextReturns[0]
Assert-Contract ($contextReturn.Pipeline -is [System.Management.Automation.Language.PipelineAst] -and
    $contextReturn.Pipeline.PipelineElements.Count -eq 1 -and
    $contextReturn.Pipeline.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) "Get-PackageContext return is not one direct expression"
$returnConversion = $contextReturn.Pipeline.PipelineElements[0].Expression
Assert-Contract ($returnConversion -is [System.Management.Automation.Language.ConvertExpressionAst] -and
    [string]$returnConversion.Type.TypeName.FullName -ceq "pscustomobject" -and
    $returnConversion.Child -is [System.Management.Automation.Language.HashtableAst]) "Get-PackageContext return is not one direct PSCustomObject hashtable"
$returnTables = @($returnConversion.FindAll({ param($node) $node -is [System.Management.Automation.Language.HashtableAst] }, $true))
Assert-Contract ($returnTables.Count -eq 1 -and [object]::ReferenceEquals($returnTables[0], $returnConversion.Child)) "Get-PackageContext return must contain one hashtable"
$returnFields = @{}
foreach ($pair in $returnTables[0].KeyValuePairs) {
    Assert-Contract (
        $pair.Item1 -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        ![string]::IsNullOrWhiteSpace([string]$pair.Item1.Value)
    ) "Get-PackageContext return key is not a direct string"
    $normalizedKey = ([string]$pair.Item1.Value).ToLowerInvariant()
    Assert-Contract (!$returnFields.ContainsKey($normalizedKey)) ("Get-PackageContext duplicate return key: " + [string]$pair.Item1.Value)
    $returnFields[$normalizedKey] = $pair.Item2
}
Assert-Contract (
    $returnFields.ContainsKey("operationsroot") -and
    (Test-ContractExactGetFullPathVariable $returnFields["operationsroot"] "operationsRoot")
) "Get-PackageContext OperationsRoot field is not exactly bound to operationsRoot"
Assert-Contract (
    $returnFields.ContainsKey("servicescript") -and
    (Test-ContractExactPipelineVariable $returnFields["servicescript"] "serviceScript")
) "Get-PackageContext ServiceScript field is not exactly bound to serviceScript"
Assert-Contract (
    $returnFields.ContainsKey("initializescript") -and
    (Test-ContractExactPipelineVariable $returnFields["initializescript"] "initializeScript")
) "Get-PackageContext InitializeScript field is not exactly bound to initializeScript"

$hardcodedOperationFallbacks = @(
    $controller.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            [string]$node.Value -ceq "data\local\operations"
        },
        $true
    )
)
Assert-Contract ($hardcodedOperationFallbacks.Count -eq 2) "controller must contain exactly two source/v1 operations fallbacks"
foreach ($literal in $hardcodedOperationFallbacks) {
    $command = $literal.Parent
    $pipeline = if ($command -is [System.Management.Automation.Language.CommandAst]) { $command.Parent } else { $null }
    $assignment = if ($pipeline -is [System.Management.Automation.Language.PipelineAst]) { $pipeline.Parent } else { $null }
    Assert-Contract ($assignment -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $assignment.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and $assignment.Left.VariablePath.UserPath -ceq "operationsRoot" -and
        (Test-ContractExactJoinPathVariableString $pipeline "resolvedRoot" "data\local\operations") -and
        (Test-ContractDescendantOf $assignment $contextFunction.Body)) "hardcoded operations fallback escaped Get-PackageContext"
}
$lockAssignments = @(Get-ContractAssignments $lockFunction.Body "lockPath")
Assert-Contract (
    $lockAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($lockAssignments[0].Parent, $lockFunction.Body.EndBlock) -and
    (Test-ContractExactJoinPathMemberString $lockAssignments[0].Right "context.OperationsRoot" ".start.lock")
) "operation lock is not one direct exact context.OperationsRoot assignment"

$serviceAssignments = @(Get-ContractAssignments $contextFunction.Body "serviceScript")
Assert-Contract (
    $serviceAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($serviceAssignments[0].Parent, $contextFunction.Body.EndBlock)
) "serviceScript must have exactly one direct assignment"
Assert-Contract ($serviceAssignments[0].Right -is [System.Management.Automation.Language.IfStatementAst]) "serviceScript assignment must select by component"
$serviceSelector = $serviceAssignments[0].Right
Assert-Contract (
    $serviceSelector.Clauses.Count -eq 1 -and
    (Test-ContractExactServiceCondition $serviceSelector.Clauses[0].Item1)
) "serviceScript selector must test exactly component == tts-more"
Assert-Contract (
    Test-ContractExactStatementBlockJoinPath $serviceSelector.Clauses[0].Item2 "resolvedRoot" "scripts\start-production.ps1"
) "TTS More serviceScript branch is not one direct start-production Join-Path"
Assert-Contract (
    Test-ContractExactStatementBlockJoinPath $serviceSelector.ElseClause "bundle" "Start-Worker.ps1"
) "fork serviceScript branch is not one direct controlled worker Join-Path"

$delegates = @(Get-ContractCommands $serviceFunction.Body "Invoke-ChildPowerShell")
Assert-Contract ($delegates.Count -eq 1) "Invoke-ServiceStart must delegate exactly once"
$delegateAssignments = @(Get-ContractAssignments $serviceFunction.Body "result")
Assert-Contract (
    $delegateAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($delegateAssignments[0].Parent, $serviceFunction.Body.EndBlock)
) "Invoke-ServiceStart delegate assignment is not a direct function statement"
Assert-Contract (
    $delegateAssignments[0].Right -is [System.Management.Automation.Language.PipelineAst] -and
    [object]::ReferenceEquals($delegates[0].Parent, $delegateAssignments[0].Right)
) "Invoke-ServiceStart delegate is not the direct result pipeline"
$delegateScript = Get-ContractParameterArgument $delegates[0] "Script"
$delegateArguments = Get-ContractParameterArgument $delegates[0] "Arguments"
Assert-Contract ((Get-ContractMemberPath $delegateScript) -ceq "context.ServiceScript") "Invoke-ServiceStart does not pass context.ServiceScript"
Assert-Contract (Test-ContractVariable $delegateArguments "arguments") "Invoke-ServiceStart does not pass the worker arguments array"
$serviceCalls = @(Get-ContractCommands $mainTry.Body "Invoke-ServiceStart")
Assert-Contract (
    $serviceCalls.Count -eq 1 -and
    $serviceCalls[0].Parent -is [System.Management.Automation.Language.PipelineAst] -and
    [object]::ReferenceEquals($serviceCalls[0].Parent.Parent, $mainTry.Body)
) "the controller main flow does not call Invoke-ServiceStart as a direct try statement"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "Root") "root") "main service call does not pass root"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "Operation") "operation") "main service call does not pass operation"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "PortOverride") "PortOverride") "main service call does not pass PortOverride"

$operationRootAssignments = @(Get-ContractAssignments $initializeOperationFunction.Body "operationRoot")
Assert-Contract (
    $operationRootAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($operationRootAssignments[0].Parent, $initializeOperationFunction.Body.EndBlock)
) "Initialize-Operation operationRoot is not one direct assignment"
Assert-Contract (
    Test-ContractExactGetFullPathJoinMemberChildVariable $operationRootAssignments[0].Right "context.OperationsRoot" "canonicalId"
) "operationRoot is not exactly GetFullPath(Join-Path context.OperationsRoot canonicalId)"

$boundaryIfs = @($initializeOperationFunction.Body.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and $_.Clauses.Count -eq 1 -and @(Get-ContractCommands $_.Clauses[0].Item1 "Test-PathWithinRoot").Count -eq 1
})
Assert-Contract ($boundaryIfs.Count -eq 1) "Initialize-Operation boundary check is not one direct if statement"
$boundaryIf = $boundaryIfs[0]
$boundaryCondition = $boundaryIf.Clauses[0].Item1
Assert-Contract (
    Test-ContractExactBoundaryCondition $boundaryCondition "operationRoot" "context.OperationsRoot"
) "Initialize-Operation boundary condition is not the exact negated containment OR negated parent equality"
Assert-Contract (
    $null -eq $boundaryIf.ElseClause -and
    (Test-ContractExactThrowBody $boundaryIf.Clauses[0].Item2 "PACKAGE_CORRUPT")
) "Initialize-Operation boundary guard does not directly throw PACKAGE_CORRUPT"

$operationContractTries = @($initializeOperationFunction.Body.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.TryStatementAst] -and @(Get-ContractCommands $_.Body "Assert-PortableExactOperationContract").Count -eq 1
})
Assert-Contract ($operationContractTries.Count -eq 1) "Initialize-Operation operation contract is not one direct try statement"
$operationContractCommands = @(Get-ContractCommands $operationContractTries[0].Body "Assert-PortableExactOperationContract")
Assert-Contract (
    $operationContractCommands.Count -eq 1 -and
    (Test-ContractDirectCommandInTry $operationContractCommands[0] $operationContractTries[0])
) "operation contract call is nested or unreachable"
Assert-Contract (
    (Get-ContractMemberPath (Get-ContractParameterArgument $operationContractCommands[0] "OperationsRoot")) -ceq "context.OperationsRoot"
) "operation contract does not use context.OperationsRoot"
Assert-Contract (
    Test-ContractVariable (Get-ContractParameterArgument $operationContractCommands[0] "OperationRoot") "operationRoot"
) "operation contract does not use operationRoot"

$activePointerLiterals = @(
    $controller.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            [string]$node.Value -ceq "active-start.json"
        },
        $true
    )
)
Assert-Contract ($activePointerLiterals.Count -eq 3) "controller must have exactly Wait/Clear/main active pointer references"
$allActivePathAssignments = @(Get-ContractAssignments $controller "activePath")
$mainActivePathAssignments = @($allActivePathAssignments | Where-Object { [object]::ReferenceEquals($_.Parent, $mainTry.Body) })
Assert-Contract (
    $mainActivePathAssignments.Count -eq 1 -and
    (Test-ContractExactJoinPathMemberString $mainActivePathAssignments[0].Right "script:Context.OperationsRoot" "active-start.json")
) "main activePath is not one exact direct context.OperationsRoot assignment"
$waitActivePathAssignments = @(Get-ContractAssignments $waitFunction.Body "activePath")
Assert-Contract (
    $waitActivePathAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($waitActivePathAssignments[0].Parent, $waitFunction.Body.EndBlock) -and
    (Test-ContractExactJoinPathMemberString $waitActivePathAssignments[0].Right "Context.OperationsRoot" "active-start.json")
) "Wait-ForActiveOperation activePath is not one exact direct Context.OperationsRoot assignment"
$clearPointerAssignments = @(Get-ContractAssignments $clearFunction.Body "pointer")
Assert-Contract (
    $clearPointerAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($clearPointerAssignments[0].Parent, $clearFunction.Body.EndBlock) -and
    (Test-ContractExactJoinPathMemberString $clearPointerAssignments[0].Right "Context.OperationsRoot" "active-start.json")
) "Clear-StaleActivePointer pointer is not one exact direct Context.OperationsRoot assignment"

$waitOperationAssignments = @(Get-ContractAssignments $waitFunction.Body "operation")
Assert-Contract (
    $waitOperationAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($waitOperationAssignments[0].Parent, $waitFunction.Body.EndBlock) -and
    (Test-ContractExactGetFullPathJoinMemberVariable $waitOperationAssignments[0].Right "Context.OperationsRoot" "parsed")
) "Wait-ForActiveOperation operation is not exactly derived from Context.OperationsRoot and parsed.ToString()"
$waitParentChecks = @($waitFunction.Body.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and $_.Clauses.Count -eq 1 -and
    (Test-ContractExactParentBoundaryCondition $_.Clauses[0].Item1 "operation" "Context.OperationsRoot")
})
Assert-Contract ($waitParentChecks.Count -eq 1) "Wait-ForActiveOperation does not directly enforce the operation parent"
$waitParentCheck = $waitParentChecks[0]
Assert-Contract (
    $null -eq $waitParentCheck.ElseClause -and
    (Test-ContractExactThrowBody $waitParentCheck.Clauses[0].Item2 "OPERATION_ACTIVE")
) "Wait-ForActiveOperation parent guard does not directly throw OPERATION_ACTIVE"

$clearTries = @($clearFunction.Body.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] })
Assert-Contract ($clearTries.Count -eq 1) "Clear-StaleActivePointer must have one direct validation try"
$clearTry = $clearTries[0]
$staleOperationAssignments = @(Get-ContractAssignments $clearFunction.Body "staleOperation")
Assert-Contract (
    $staleOperationAssignments.Count -eq 1 -and
    [object]::ReferenceEquals($staleOperationAssignments[0].Parent, $clearTry.Body) -and
    (Test-ContractExactJoinPathMemberInvocation $staleOperationAssignments[0].Right "Context.OperationsRoot" "parsed" "ToString")
) "Clear-StaleActivePointer staleOperation is not one direct Context.OperationsRoot child"
$clearContracts = @(Get-ContractCommands $clearTry.Body "Assert-PortableExactOperationContract")
Assert-Contract ($clearContracts.Count -eq 1 -and (Test-ContractDirectCommandInTry $clearContracts[0] $clearTry)) "Clear-StaleActivePointer exact contract is nested or missing"
Assert-Contract (
    (Get-ContractMemberPath (Get-ContractParameterArgument $clearContracts[0] "OperationsRoot")) -ceq "Context.OperationsRoot"
) "Clear-StaleActivePointer contract does not use Context.OperationsRoot"
Assert-Contract (
    Test-ContractVariable (Get-ContractParameterArgument $clearContracts[0] "OperationRoot") "staleOperation"
) "Clear-StaleActivePointer contract does not validate staleOperation"

$pythonAssignments = @(Get-ContractAssignments $worker "Python")
Assert-Contract ($pythonAssignments.Count -eq 1) "worker Python must have exactly one active assignment"
Assert-Contract (Test-ContractTopLevelAssignment $pythonAssignments[0]) "worker Python assignment is not an active top-level statement"
Assert-Contract ($pythonAssignments[0].Right -is [System.Management.Automation.Language.PipelineAst]) "worker Python assignment is not a direct executable pipeline"
Assert-Contract (Test-ContractJoinPath $pythonAssignments[0].Right "Root" "runtime\live\python.exe") "worker Python is not package-private runtime/live/python.exe"

$argumentAssignments = @(Get-ContractAssignments $worker "arguments")
Assert-Contract ($argumentAssignments.Count -eq 1) "worker arguments must have exactly one active assignment"
Assert-Contract (Test-ContractTopLevelAssignment $argumentAssignments[0]) "worker arguments assignment is not an active top-level statement"
Assert-Contract ($argumentAssignments[0].Right -is [System.Management.Automation.Language.CommandExpressionAst]) "worker arguments assignment is not a direct array expression"
$argumentStrings = @(
    $argumentAssignments[0].Right.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.StringConstantExpressionAst] },
        $true
    ) | ForEach-Object { [string]$_.Value }
)
Assert-Contract ($argumentStrings -ccontains "-m" -and $argumentStrings -ccontains "uvicorn") "worker arguments do not execute uvicorn as a module"
Assert-Contract (Test-ContractContainsMemberPath $argumentAssignments[0].Right "config.module") "worker arguments do not use the configured worker module"

$startArgumentLineAssignments = @(Get-ContractAssignments $worker "startArgumentLine")
Assert-Contract ($startArgumentLineAssignments.Count -eq 1) "worker serialized argument line must have exactly one active assignment"
Assert-Contract (Test-ContractTopLevelAssignment $startArgumentLineAssignments[0]) "worker serialized argument line assignment is not an active top-level statement"
$serializers = @(Get-ContractCommands $startArgumentLineAssignments[0].Right "ConvertTo-PortableWindowsArgumentLine")
Assert-Contract ($serializers.Count -eq 1) "worker does not use the shared Windows argument serializer exactly once"
$serializerArguments = Get-ContractParameterArgument $serializers[0] "Arguments"
Assert-Contract (Test-ContractVariable $serializerArguments "arguments") "worker serializer does not consume the logical uvicorn arguments"

$processAssignments = @(Get-ContractAssignments $worker "process")
$startAssignments = @($processAssignments | Where-Object { @(Get-ContractCommands $_.Right "Start-Process").Count -eq 1 })
$nullAssignments = @($processAssignments | Where-Object {
    (Test-ContractTopLevelAssignment $_) -and
    (Test-ContractVariable (Get-ContractExactExpression $_.Right) "null")
})
Assert-Contract (
    $processAssignments.Count -eq 2 -and
    $startAssignments.Count -eq 1 -and
    $nullAssignments.Count -eq 1
) "worker process identity is not initialized once and started once"
$startupAssignment = $startAssignments[0]
Assert-Contract ($startupAssignment.Right -is [System.Management.Automation.Language.PipelineAst]) "worker process assignment is not a direct executable pipeline"
$startupTryBody = $startupAssignment.Parent
$startupTry = $startupTryBody.Parent
Assert-Contract (
    $startupTryBody -is [System.Management.Automation.Language.StatementBlockAst] -and
    $startupTry -is [System.Management.Automation.Language.TryStatementAst] -and
    [object]::ReferenceEquals($startupTry.Parent, $worker.EndBlock)
) "worker process assignment is not directly guarded by one top-level startup transaction"
$rollbackLiterals = @($startupTry.CatchClauses | ForEach-Object {
    $_.Body.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            [string]$node.Value -ceq "rollback-started-process"
        },
        $true
    )
})
Assert-Contract ($rollbackLiterals.Count -eq 1) "worker startup transaction does not contain one rollback command"
$startProcesses = @(Get-ContractCommands $startupAssignment.Right "Start-Process")
Assert-Contract ($startProcesses.Count -eq 1) "worker must start exactly one service process from the guarded process assignment"
$filePath = Get-ContractParameterArgument $startProcesses[0] "FilePath"
$argumentList = Get-ContractParameterArgument $startProcesses[0] "ArgumentList"
$workingDirectory = Get-ContractParameterArgument $startProcesses[0] "WorkingDirectory"
Assert-Contract (Test-ContractVariable $filePath "Python") "worker service process does not use package-private Python"
Assert-Contract (Test-ContractVariable $argumentList "startArgumentLine") "worker service process does not use the serialized uvicorn argument line"
Assert-Contract (Test-ContractVariable $workingDirectory "SourceRoot") "worker service process does not use the resolved upstream source root"

$forbiddenParameters = @(
    $worker.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.ParameterAst] -and
            $node.Name.VariablePath.UserPath -match "(?i)python"
        },
        $true
    )
)
$forbiddenOverrides = @(
    $worker.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $node.VariablePath.UserPath -ceq "env:TTS_MORE_PYTHON_EXE"
        },
        $true
    )
)
$forbiddenStrings = @(
    $worker.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            (
                [string]$node.Value -match "(?i)\.venv" -or
                (
                    $node.Parent -isnot [System.Management.Automation.Language.MemberExpressionAst] -and
                    [string]$node.Value -in @("python", "python.exe", "py", "py.exe")
                )
            )
        },
        $true
    )
)
Assert-Contract ($forbiddenParameters.Count -eq 0) "worker accepts a Python override parameter"
Assert-Contract ($forbiddenOverrides.Count -eq 0) "worker accepts TTS_MORE_PYTHON_EXE"
Assert-Contract ($forbiddenStrings.Count -eq 0) "worker contains a system/.venv Python fallback"

Write-Output "PORTABLE_CONTROL_FLOW_AST_OK"
"""


def _powershell_executable(platform_name: str | None = None) -> str:
    platform_name = platform_name or os.name
    system_root = os.environ.get("SystemRoot")
    if platform_name == "nt" and system_root:
        windows_powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if windows_powershell.is_file():
            return str(windows_powershell)
    candidates = ("powershell.exe", "pwsh") if platform_name == "nt" else ("pwsh",)
    for name in candidates:
        executable = shutil.which(name)
        if executable:
            return executable
    raise AssertionError("PowerShell 5.1 or PowerShell 7 is required for the portable integration contract")


def _verify_powershell_control_flow(bundle: Path) -> None:
    environment = os.environ.copy()
    environment["TTS_MORE_CONTRACT_CONTROLLER"] = str(bundle / "Invoke-PortableStart.ps1")
    environment["TTS_MORE_CONTRACT_WORKER"] = str(bundle / "Start-Worker.ps1")
    with tempfile.TemporaryDirectory(prefix="tts-more-contract-") as directory:
        verifier = Path(directory) / "verify-portable-control-flow.ps1"
        verifier.write_text(_POWERSHELL_SEMANTIC_CONTRACT, encoding="utf-8-sig")
        completed = subprocess.run(
            [
                _powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(verifier),
            ],
            env=environment,
            capture_output=True,
            check=False,
        )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise AssertionError(f"PowerShell control-flow contract failed:\n{stdout}\n{stderr}")
    if "PORTABLE_CONTROL_FLOW_AST_OK" not in stdout:
        raise AssertionError(f"PowerShell control-flow contract returned no success marker:\n{stdout}\n{stderr}")


class PortableIntegrationContractTests(unittest.TestCase):
    def test_powershell_resolver_prefers_windows_51_and_falls_back_to_pwsh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system_root = Path(directory)
            windows_powershell = (
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            windows_powershell.parent.mkdir(parents=True)
            windows_powershell.touch()
            with mock.patch.dict(os.environ, {"SystemRoot": str(system_root)}, clear=True):
                with mock.patch("shutil.which", return_value="C:/Tools/pwsh.exe"):
                    self.assertEqual(str(windows_powershell), _powershell_executable("nt"))

        def find_pwsh(name: str) -> str | None:
            return "/usr/bin/pwsh" if name == "pwsh" else None

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", side_effect=find_pwsh):
                self.assertEqual("/usr/bin/pwsh", _powershell_executable("posix"))
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(AssertionError, "PowerShell 5.1 or PowerShell 7"):
                    _powershell_executable("posix")

    def test_git_tracked_paths_decodes_utf8_without_locale_dependency(self) -> None:
        expected = {"Start.cmd", "使用说明-先看这里.txt"}
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="Start.cmd\n使用说明-先看这里.txt\n".encode("utf-8"),
            stderr=b"",
        )

        with mock.patch("subprocess.run", return_value=completed) as run:
            tracked = _git_tracked_paths(ROOT, expected)

        self.assertEqual(expected, tracked)
        self.assertIn("core.quotePath=false", run.call_args.args[0])
        self.assertNotIn("text", run.call_args.kwargs)
        self.assertNotIn("encoding", run.call_args.kwargs)

    def test_controlled_mirror_has_no_hash_drift(self) -> None:
        manifest = json.loads((BUNDLE / "integration.manifest.json").read_text(encoding="utf-8"))
        expected = manifest["files"]
        for relative, digest in expected.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            canonical = path.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest, relative)
        controlled = {
            path.relative_to(ROOT).as_posix()
            for path in BUNDLE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != "integration.manifest.json"
        }
        self.assertEqual(controlled, {name for name in expected if name.startswith("tts_more/")})
        tracked = _git_tracked_paths(ROOT, expected)
        self.assertEqual(set(expected), tracked, "controlled integration files must be Git tracked")

    def test_package_entrypoints_and_native_webui_are_separate(self) -> None:
        for name in (
            "Initialize.cmd",
            "Start.cmd",
            "Stop.cmd",
            "Repair.cmd",
            "Build-Package.ps1",
            "Start-WebUI.cmd",
            "使用说明-先看这里.txt",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        start_path = ROOT / "Start.cmd"
        start = start_path.read_text(encoding="utf-8")
        webui = (ROOT / "Start-WebUI.cmd").read_text(encoding="utf-8")
        self.assertEqual(
            [
                'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                '"%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
                "exit /b %errorlevel%",
            ],
            _active_cmd_lines(start_path),
        )
        self.assertNotEqual(start, webui)

    def test_controller_uses_manifest_operations_worker_delegate_and_private_runtime(self) -> None:
        _verify_powershell_control_flow(BUNDLE)

    def test_operation_protocol_is_controlled(self) -> None:
        self.assertTrue((BUNDLE / "portable_operations.py").is_file(), "portable operation protocol")

    def test_previous_version_import_tools_are_controlled(self) -> None:
        for relative in (
            "import_portable_data.py",
            "import-portable-data.py",
            "select-portable-folder.ps1",
        ):
            self.assertTrue((BUNDLE / relative).is_file(), relative)
        launcher = (BUNDLE / "Invoke-PortableStart.ps1").read_text(encoding="utf-8")
        selector = (BUNDLE / "select-portable-folder.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-PortableImportOffer", launcher)
        self.assertIn("Assert-PortableSha256Manifest", launcher)
        self.assertNotIn("Read-Host", launcher)
        for display_name in ("TTS More", "GPT-SoVITS", "IndexTTS", "CosyVoice"):
            self.assertIn(display_name, selector)

    def test_model_and_device_locks_are_complete_and_immutable(self) -> None:
        model_lock = json.loads((BUNDLE / "locks" / "models.lock.json").read_text(encoding="utf-8"))
        self.assertTrue(model_lock["complete"], model_lock["missing_required_paths"])
        targets = {asset["target"] for asset in model_lock["assets"]}
        self.assertTrue(set(model_lock["required_paths"]) <= targets)
        for asset in model_lock["assets"]:
            self.assertRegex(asset["source_revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(asset["size_bytes"], 0)
            self.assertGreaterEqual(len(asset["urls"]), 2)
            routes = set()
            for url in asset["urls"]:
                parsed = urlsplit(url)
                host = str(parsed.hostname or "").lower()
                parts = [part for part in parsed.path.split("/") if part]
                repository = tuple(parts[:2]) if host in {"huggingface.co", "hf-mirror.com"} else ()
                routes.add((host, *repository))
            self.assertGreaterEqual(len(routes), 2)
            self.assertTrue(any(asset["source_revision"] in url for url in asset["urls"]))
            self.assertTrue(all(re.search(r"/resolve/[0-9a-f]{40}/", url) for url in asset["urls"]))
        if model_lock["component"] == "cosyvoice":
            self.assertNotIn("README.md", {asset["source_path"] for asset in model_lock["assets"]})
            self.assertEqual(
                {
                    "license": "Apache-2.0",
                    "runtime_required": False,
                    "sha256": "97b420f4afcbbce667623a882439d5ee1a64a2f33d5023a942bc411862cccf0c",
                    "size_bytes": 10116,
                    "source_path": "README.md",
                    "source_revision": "d979372752f86be76f2b798435a0f1593bfddb4e",
                    "source_url": (
                        "https://www.modelscope.cn/models/iic/CosyVoice-300M/resolve/"
                        "d979372752f86be76f2b798435a0f1593bfddb4e/README.md"
                    ),
                },
                model_lock["documentation_provenance"],
            )
        for profile in ("cpu", "cu126", "cu128"):
            contents = (BUNDLE / "locks" / f"requirements-{profile}.lock.txt").read_text(encoding="utf-8")
            starts = list(re.finditer(r"(?m)^[A-Za-z0-9_.-]+==[^\s\\]+", contents))
            self.assertTrue(starts, profile)
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(contents)
                self.assertIn("--hash=sha256:", contents[start.start():end], start.group(0))

    def test_runtime_lock_assets_have_two_independent_download_routes(self) -> None:
        runtime_lock = json.loads((BUNDLE / "locks" / "runtime.lock.json").read_text(encoding="utf-8"))
        assets = list(runtime_lock["assets"].values()) + list(runtime_lock.get("payloads", []))
        for asset in assets:
            self.assertGreaterEqual(len(asset["urls"]), 2, asset["id"])
            routes = set()
            for url in asset["urls"]:
                parsed = urlsplit(url)
                host = str(parsed.hostname or "").lower()
                parts = [part for part in parsed.path.split("/") if part]
                repository = tuple(parts[:2]) if host in {"huggingface.co", "hf-mirror.com"} else ()
                routes.add((host, *repository))
            self.assertGreaterEqual(len(routes), 2, asset["id"])

    def test_full_release_is_fail_closed_in_github_actions(self) -> None:
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")
        self.assertIn('$env:GITHUB_ACTIONS -eq "true"', builder)
        self.assertIn("audit-release --zip", builder)

    def test_worker_initializer_uses_locked_embedded_python_and_uv_in_required_order(self) -> None:
        worker_bundle = BUNDLE if BUNDLE.is_dir() else ROOT / "integrations" / "windows"
        initializer = (worker_bundle / "Initialize.ps1").read_text(encoding="utf-8")
        required = (
            '. (Join-Path $Bundle "portable-python.ps1")',
            "Install-PortablePythonRuntime",
            "platform.python_version()",
            "select-device",
            "$runtimeLock.payloads",
            "$modelLock.assets",
            "--target",
            "--link-mode",
            '"copy"',
            "pip check",
            "Invoke-PortablePythonSourceProbe -Root $Root -SourceRoot $SourceRoot -PythonPath $PortableRuntime.Python -ImportProbe $importProbe",
        )
        for token in required:
            self.assertIn(token, initializer, token)
        positions = [initializer.index(token) for token in required]
        self.assertEqual(positions, sorted(positions))
        publish_call = initializer.rindex("Publish-PortableRuntimeTransaction")
        self.assertGreater(publish_call, initializer.index(required[-1]))
        self.assertGreater(initializer.rindex("write-state"), publish_call)
        self.assertNotIn("& $PortableRuntime.Python -c $importProbe", initializer)
        for forbidden in (
            "bootstrap-conda.ps1",
            "conda create",
            "$Conda",
            "$BootstrapPython",
            "-m pip",
            "Scripts\\uv.exe",
            "base_prefix",
        ):
            self.assertNotIn(forbidden, initializer)
        self.assertIn("$PortableRuntime.Python", initializer)
        self.assertIn("$PortableRuntime.Uv", initializer)
        self.assertIn("$PortableRuntime.SitePackages", initializer)
        self.assertIn("function Publish-PortableRuntimeTransaction", initializer)

    def test_package_private_python_entrypoints_disable_bytecode_writes(self) -> None:
        entrypoints = (
            {name: BUNDLE / name for name in (
                "Invoke-PortableStart.ps1",
                "Initialize.ps1",
                "Start-Worker.ps1",
                "Stop-Worker.ps1",
                "Start-WebUI.ps1",
            )}
            if BUNDLE.is_dir()
            else {
                "Invoke-PortableStart.ps1": ROOT / "scripts" / "Invoke-PortableStart.ps1",
                **{
                    name: ROOT / "integrations" / "windows" / name
                    for name in (
                        "Initialize.ps1",
                        "Start-Worker.ps1",
                        "Stop-Worker.ps1",
                        "Start-WebUI.ps1",
                    )
                },
            }
        )
        for name, path in entrypoints.items():
            script = path.read_text(encoding="utf-8")
            self.assertIn('$env:PYTHONDONTWRITEBYTECODE = "1"', script, name)

        webui = entrypoints["Start-WebUI.ps1"].read_text(encoding="utf-8")
        self.assertIn(
            '$arguments = @("-I", "-B", (Join-Path $SourceRoot "webui.py"), "zh_CN")',
            webui,
        )
        runner_path = (
            BUNDLE / "portable_package_runner.py"
            if BUNDLE.is_dir()
            else ROOT / "scripts" / "portable_package_runner.py"
        )
        runner = runner_path.read_text(encoding="utf-8")
        self.assertIn('worker_env["PYTHONDONTWRITEBYTECODE"] = "1"', runner)
        self.assertRegex(runner, r'str\(runtime_python\),\s*"-B",\s*"-m",')

    def test_worker_builder_stages_helper_and_applies_full_runtime_audits(self) -> None:
        worker_bundle = BUNDLE if BUNDLE.is_dir() else ROOT / "integrations" / "windows"
        builder = (worker_bundle / "Build-Package.ps1").read_text(encoding="utf-8")
        for token in (
            "portable-python.ps1",
            "UV_CACHE_DIR",
            "pyvenv.cfg",
            "conda-meta",
            "condabin",
            "Miniforge",
            "ReparsePoint",
            "NumberOfLinks",
            "machine-prefix",
        ):
            self.assertIn(token, builder, token)

    def test_worker_artifacts_and_capabilities_follow_portable_contract(self) -> None:
        component = json.loads((BUNDLE / "component.json").read_text(encoding="utf-8"))["component"]
        worker_name = {
            "gpt-sovits": "gpt_sovits_worker.py",
            "indextts": "indextts_worker.py",
            "cosyvoice": "cosyvoice_worker.py",
        }[component]
        required_capabilities = {
            "gpt-sovits": {"trained_weights_voice", "reference_audio_voice"},
            "indextts": {"reference_audio_voice", "emotion_text"},
            "cosyvoice": {"reference_audio_voice", "zero_shot_voice", "cross_lingual_voice"},
        }[component]
        starter = (BUNDLE / "Start-Worker.ps1").read_text(encoding="utf-8")
        worker = (BUNDLE / "app" / "workers" / worker_name).read_text(encoding="utf-8")
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")

        self.assertIn(
            '$env:TTS_MORE_ARTIFACT_ROOT = (Join-Path $Root "data\\local\\artifacts")',
            starter,
        )
        self.assertIn('os.environ.get("TTS_MORE_ARTIFACT_ROOT")', worker)
        for capability in required_capabilities:
            self.assertIn(f'"{capability}"', worker)
        mapping = next(
            line.strip()
            for line in builder.splitlines()
            if line.strip().startswith(f'"{component}" {{ @(')
        )
        for capability in required_capabilities:
            self.assertIn(f'"{capability}"', mapping)

    def test_worker_stop_accepts_exact_patch_and_legacy_runtime_locks(self) -> None:
        worker_bundle = BUNDLE if BUNDLE.is_dir() else ROOT / "integrations" / "windows"
        stopper = (worker_bundle / "Stop-Worker.ps1").read_text(encoding="utf-8")
        self.assertIn('@("3.10", "3.10.11", "3.11", "3.11.9")', stopper)

    def test_portable_installer_operation_progress_is_bundle_relative(self) -> None:
        installer = ((BUNDLE if BUNDLE.is_dir() else ROOT / "scripts") / "portable_install.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.path.insert(0, str(Path(__file__).resolve().parent))", installer)
        self.assertIn('Path(__file__).resolve().with_name("portable_operations.py")', installer)
        self.assertIn("importlib.util.spec_from_file_location", installer)
        self.assertIn("append_event = operations.append_event", installer)
        self.assertNotIn("from scripts.portable_operations import append_event", installer)

    def test_bootstrap_builder_removes_and_rejects_t7_model_weights(self) -> None:
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")
        self.assertEqual(2, len(re.findall(r"safetensors\|ckpt\|pth\|pt\|t7\|onnx\|bin", builder)))

    def test_bootstrap_builder_recursively_excludes_and_rejects_git_metadata(self) -> None:
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")
        self.assertIn('$entry.Name -in $ExcludedNames', builder)
        self.assertIn('$_.Name -eq ".git" -or', builder)


if __name__ == "__main__":
    unittest.main()
