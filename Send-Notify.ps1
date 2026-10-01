param(
    [Parameter(Mandatory=$true, Position=0)][string]$Message,
    [string]$Title = 'Notification',
    [string]$Source = 'agent',
    [ValidateRange(1,5)][int]$Priority = 3
)
& python.exe "$PSScriptRoot\notify.py" send $Message --title $Title --source $Source --priority $Priority
exit $LASTEXITCODE
