(() => {
  const labels = new Set([
    '生成方案','扫描市场','查看持仓','复盘','绩效','查行情','新闻风险',
    'BTC','ETH','SOL','BNB','XRP','DOGE'
  ]);
  function mountQuickActions() {
    const buttons = [...document.querySelectorAll('button')]
      .filter((b) => labels.has((b.innerText || '').trim()));
    if (buttons.length < 10) return;
    const group = buttons[0].parentElement;
    if (group && group.id !== 'crypto-quick-actions') {
      group.id = 'crypto-quick-actions';
    }
  }
  mountQuickActions();
  new MutationObserver(mountQuickActions).observe(document.body, {
    childList: true,
    subtree: true
  });
})();