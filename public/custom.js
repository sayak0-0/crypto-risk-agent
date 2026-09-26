(() => {
  const labels = new Set([
    '生成方案','适合开仓','账户余额','查看持仓','挂单/止盈止损','复盘','绩效','查行情','新闻风险','扫描市场',
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
  function esc(x) {
    return String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  async function loadPositions() {
    try {
      const r = await fetch('/public/positions.json?t=' + Date.now(), {cache:'no-store'});
      if (!r.ok) return;
      const rows = await r.json();
      let panel = document.getElementById('crypto-position-watch');
      if (!rows || !rows.length) { if (panel) panel.remove(); return; }
      if (!panel) { panel = document.createElement('div'); panel.id = 'crypto-position-watch'; document.body.appendChild(panel); }
      panel.innerHTML = '<div class="pw-title">持仓观察 · 24小时</div>' + rows.map(x => {
        const pnl = Number(x['浮动盈亏'] || 0), roi = Number(x['保证金收益率'] || 0);
        const cls = pnl > 0 ? 'pw-up' : pnl < 0 ? 'pw-down' : '';
        return `<div class="pw-item"><div class="pw-head"><b>${esc(x['币种'])} ${esc(x['方向中文'])} ${esc(x['杠杆'])}x</b><span class="${cls}">${pnl>=0?'+':''}${pnl.toFixed(4)} U</span></div><div class="pw-meta">${esc(x['观察模式']||'实盘')} · 入场 ${esc(x['入场价'])} · 现价 ${esc(x['当前价'] ?? '等待')} · ${roi>=0?'+':''}${roi.toFixed(2)}%</div><div class="pw-meta">${esc(x['状态'])} · 行情${esc(x['行情状态']||'正常')} · 下次 ${esc(String(x['下次检查']||'').slice(0,16))}</div></div>`;
      }).join('');
    } catch (_) {}
  }
  mountQuickActions();
  loadPositions();
  setInterval(loadPositions, 60000);
  new MutationObserver(mountQuickActions).observe(document.body, {
    childList: true,
    subtree: true
  });
})();