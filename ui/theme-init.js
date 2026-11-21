// 在主界面模块加载前同步恢复主题，避免首次绘制闪烁；外部脚本兼容严格 CSP。
(function () {
  try {
    var theme = localStorage.getItem('ui_theme') === 'light' ? 'light' : 'dark'
    document.documentElement.setAttribute('data-theme', theme)
    document.documentElement.style.colorScheme = theme
  } catch (_) {}
})()
