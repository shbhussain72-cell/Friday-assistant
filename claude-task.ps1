param([string]$TaskFile, [string]$PromptFile)
# Opened by Jarvis: an interactive Claude Code session with Chrome, seeded with the spoken task.
$task = if ($TaskFile -and (Test-Path $TaskFile)) { (Get-Content -Raw $TaskFile).Trim() } else { "" }
$Host.UI.RawUI.WindowTitle = "Jarvis -> Claude"
if ($task) {
    Write-Host "Jarvis task: $task`n" -ForegroundColor Cyan
    claude --chrome --append-system-prompt-file $PromptFile $task
} else {
    claude --chrome --append-system-prompt-file $PromptFile
}
