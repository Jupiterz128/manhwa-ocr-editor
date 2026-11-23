# Inspect only visible Adobe payment windows in this interactive session.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$ui = [Windows.Automation.AutomationElement]
$tree = [Windows.Automation.TreeScope]
$root = $ui::RootElement
$named = [Windows.Automation.PropertyCondition]::new($ui::NameProperty, 'Photoshop')
foreach ($win in $root.FindAll($tree::Children, $named)) {
    $p = Get-Process -Id $win.Current.ProcessId -ErrorAction SilentlyContinue
    if (!$p -or !$p.Path -or $p.Path -notmatch '\\(?:Program Files(?: \(x86\))?\\(?:Common Files\\)?Adobe)\\') { continue }
    if ($win.Current.ClassName -eq 'Photoshop' -or $win.Current.IsOffscreen) { continue }
    # Require independent markers from the billing form before using its normal Close action.
    $matchesForm = $true
    foreach ($label in 'Your payment method','Retry this payment method','Your plan') {
        $marker = [Windows.Automation.PropertyCondition]::new($ui::NameProperty, $label)
        if (!$win.FindFirst($tree::Descendants, $marker)) { $matchesForm = $false; break }
    }
    if (!$matchesForm) { continue }
    $pattern = $null
    if ($win.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$pattern)) {
        $pattern.Close()
        exit 0
    }
}
exit 1
