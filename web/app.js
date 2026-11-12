/* ManageBac-packer —— 主逻辑
 * 由 web/split_files.py 从 index.html 内联脚本抽出（2026-09-20）。
 */
const $ = s => document.querySelector(s);
let DATA = null;
let WARM = null;                       /* 预热状态（体现「打开即有内容」） */
let DEFAULT_HOST = '';                 /* 学校网址（引导页预填 + 提示用） */
/*  占位域名 —— 后端没配过学校时给的就是它。不是任何真学校，
    绝不能拿它去发请求（以前预填进输入框，用户不改动直接提交，
    打到假域名上报「无法连接」，网址框还折着，无处改）。 */
const SETUP_PLACEHOLDER_HOST = 'your-school.managebac.cn';
let HAS_CREDENTIALS = true;            /* 是否已填过账号（false → 空状态引导去填） */
let scheduleDirty = false;             /* 时间推移导致状态变化（开始/结束上课） */
let collapsed = JSON.parse(localStorage.getItem('mb.col') || '{}');
let openCourses = JSON.parse(localStorage.getItem('mb.oc') || '{}');

const PAL = ['#4a7fb5','#8b6bb5','#3f9e8c','#d17a3e','#c46b96','#3a93a8',
             '#c99a2e','#4c9a63','#c25b50','#7fa63f','#5b8fc9','#a07fc9'];
const colorOf = i => PAL[i % PAL.length];

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

/* ══════════ 实时时间计算 ══════════
   关键：days_left 不能沿用抓取时算好的值（会随时间变旧）。
   这里每次渲染都用「今天」重新计算，保证界面永远显示正确的剩余天数。 */
function todayISO(){
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function realDaysLeft(isoDate){
  if(!isoDate) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if(!m) return null;
  const due = new Date(+m[1], +m[2]-1, +m[3]);
  const now = new Date();
  const t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((due - t0) / 86400000);
}
/* 把抓取时存的 days_left 替换成实时值 */
function liveTask(t){
  return {...t, days_left: realDaysLeft(t.due_date)};
}

/* 秒数 → 友好倒计时文案
    刷新间隔较长，显示 "299s" 很怪，
     所以按量级自动换单位。 */
function fmtCountdown(sec){
  sec = Math.max(0, Math.round(sec || 0));
  if(sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if(m < 60) return s ? `${m}m${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

/* 分数 → 颜色 */
function pctColor(p){
  if(p==null) return 'var(--dim)';
  if(p>=90) return 'var(--green)';
  if(p>=80) return 'var(--teal)';
  if(p>=70) return '#b08c2a';
  if(p>=60) return 'var(--orange)';
  return 'var(--red)';
}
const LEVEL_COLOR = {A:'var(--green)',B:'var(--cyan)',C:'var(--yellow)',
                     D:'var(--orange)',F:'var(--red)'};
function lvlColor(l){
  if(!l) return 'var(--dim)';
  return LEVEL_COLOR[l[0].toUpperCase()] || 'var(--muted)';
}

function dueTag(d){
  if(d==null) return '';
  if(d<0)  return `<span class="tag due">已过期 ${-d} 天</span>`;
  if(d===0)return `<span class="tag due">今天截止</span>`;
  if(d===1)return `<span class="tag soon">明天截止</span>`;
  if(d<=3) return `<span class="tag soon">${d} 天后</span>`;
  if(d<=7) return `<span class="tag wk">${d} 天后</span>`;
  return `<span class="tag dim">${d} 天后</span>`;
}

/* ══════════════════════════════════════════════════════════════
   打开原网页：进度提示 + 防重复 + 失败弹窗（0.5.2 补充）

   【为什么要有这一套】
     原来点下去之后界面没有任何反馈 —— 不知道在等、成功了、还是失败了。
     人一旦看不到反应就会反复点：实测一次操作里同一个链接被打开了十几次，
     Edge 被拖出一屏进程。更糟的是内部若卡住，调用干脆不返回，
     界面上就是「点了三分钟什么都没有」。

   现在：
     · 点下去立刻出现进度卡，实时写「已耗时 N 秒」
     · 上一次还没结束，再次点击直接说明「请稍候」，不会再起一次
     · 失败 / 超时 → 弹出窗口写明原因，并可「重试」
     · 降级到系统浏览器也弹窗说明（需要重新登录），不再只是一闪而过的 toast
   ══════════════════════════════════════════════════════════════ */
let OPEN_BUSY = false;        /* 是否已有一次打开在等结果 */
let OPEN_T0 = 0;              /* 本次开始时刻 */
let OPEN_TIMER = null;        /* 秒数刷新定时器 */
let OPEN_GUARD = null;        /* 前端兜底超时 */
let OPEN_RETRY_URL = '';      /* 「重试」要用的链接 */

function opShow(url){
  const el = $('#openprog');
  if(!el) return;
  const sub = $('#opSub');
  if(sub) sub.textContent = url || '';
  el.classList.add('show');
  opTick();
}

function opTick(){
  const t = $('#opTime');
  if(!t) return;
  const sec = Math.max(0, Math.floor((Date.now() - OPEN_T0) / 1000));
  t.textContent = '已耗时 ' + sec + ' 秒';
}

function opHide(){
  const el = $('#openprog');
  if(el) el.classList.remove('show');
  if(OPEN_TIMER){ clearInterval(OPEN_TIMER); OPEN_TIMER = null; }
}

/*  结果弹窗。kind: 'error'（打不开） / 'info'（打开了但有话说） */
function opDialog(cfg){
  const host = $('#opendlg');
  if(!host) return;
  const ico = $('#odIco'), title = $('#odTitle'), msg = $('#odMsg');
  const meta = $('#odMeta'), retry = $('#odRetry');
  if(ico){
    ico.className = 'odico' + (cfg.kind === 'info' ? ' info' : '');
    ico.innerHTML = icon(cfg.kind === 'info' ? 'i-external' : 'i-alert', 'sm');
  }
  if(title) title.textContent = cfg.title || '打不开原网页';
  if(msg) msg.textContent = cfg.msg || '';
  if(meta){
    if(cfg.meta){ meta.textContent = cfg.meta; meta.hidden = false; }
    else { meta.textContent = ''; meta.hidden = true; }
  }
  OPEN_RETRY_URL = cfg.retryUrl || '';
  if(retry) retry.hidden = !OPEN_RETRY_URL;
  host.classList.add('open');
  setTimeout(()=>{ $('#odOk')?.focus(); }, 60);
}

function opCloseDialog(){
  $('#opendlg')?.classList.remove('open');
}

/*  把后端返回的结果翻译成人能看懂的东西 */
function opHandleResult(r, err, url){
  if(err){
    opDialog({kind:'error', title:'打不开原网页',
      msg:'打开时出现错误：' + err, retryUrl:url});
    return;
  }
  if(!r){
    opDialog({kind:'error', title:'打不开原网页',
      msg:'没有收到结果。可能是界面与后台之间的连接断了。', retryUrl:url});
    return;
  }
  if(r.busy){
    toast(r.error || '正在打开上一个链接，请稍候', 'err', '');
    return;
  }
  if(r.ok){
    if(r.fallback){
      opDialog({kind:'info', title:'已在系统默认浏览器打开',
        msg: r.note || '需要重新登录。',
        meta: '组件自带的浏览器没能打开这个链接，因此改用了系统默认浏览器。'});
    }else{
      const s = (typeof r.elapsed === 'number')
        ? '（' + r.elapsed.toFixed(1) + ' 秒）' : '';
      toast('已打开' + s, 'ok', '');
    }
    return;
  }
  /* 失败：把「等了多久 / 为什么」都写出来 */
  const metaBits = [];
  if(typeof r.elapsed === 'number'){
    metaBits.push('本次等待 ' + r.elapsed.toFixed(1) + ' 秒');
  }
  if(r.timeout) metaBits.push('后台操作在超时时间内没有返回');
  opDialog({
    kind:'error',
    title: r.timeout ? '打开超时' : '打不开原网页',
    msg: r.error || '未知原因。',
    meta: metaBits.join(' · '),
    retryUrl: url
  });
}

function openURL(u){
  if(!u) return;

  /*  上一次还没结束：明确说明，而不是再起一次 ——
      否则用户越是着急点，进程开得越多。 */
  if(OPEN_BUSY){
    toast('上一次打开还没结束，请稍候', 'err', '');
    return;
  }

  /*  纯预览页（file://，没有桥）→ 直接开新窗口。
     这种路径也要给反馈，否则同样是「点了没反应」。 */
  if(!(hasBridge() && window.pywebview?.api?.open_url)){
    window.open(u, '_blank');
    toast('已在新窗口打开', 'ok', '');
    return;
  }

  OPEN_BUSY = true;
  OPEN_T0 = Date.now();
  opShow(u);
  OPEN_TIMER = setInterval(opTick, 250);

  const finish = (r, err) => {
    if(!OPEN_BUSY) return;                 /* 已经收过尾了 */
    OPEN_BUSY = false;
    if(OPEN_GUARD){ clearTimeout(OPEN_GUARD); OPEN_GUARD = null; }
    opHide();
    opHandleResult(r, err, u);
  };

  /*  两道保险：后端 25 秒硬超时；前端再兜 35 秒 ——
      万一桥本身卡住（连返回都没有），界面也不会永远停在「正在打开」。 */
  OPEN_GUARD = setTimeout(()=>{
    finish({ok:false, timeout:true,
            elapsed: (Date.now() - OPEN_T0) / 1000,
            error:'等待超过 35 秒仍然没有响应。'}, null);
  }, 35000);

  try{
    Promise.resolve(window.pywebview.api.open_url(u))
      .then(r => finish(r, null))
      .catch(e => finish(null, String(e)));
  }catch(e){
    finish(null, String(e));
  }
}

function bindOpenDialog(){
  if(bindOpenDialog._done) return;
  bindOpenDialog._done = true;
  $('#odClose')?.addEventListener('click', opCloseDialog);
  $('#odOk')?.addEventListener('click', opCloseDialog);
  $('#opendlg')?.addEventListener('click', e=>{
    if(e.target && e.target.id === 'opendlg') opCloseDialog();
  });
  $('#odRetry')?.addEventListener('click', ()=>{
    const u = OPEN_RETRY_URL;
    opCloseDialog();
    if(u) setTimeout(()=> openURL(u), 140);
  });
  document.addEventListener('keydown', e=>{
    if(e.key === 'Escape') opCloseDialog();
  });
}

/* ══════════ 统一数据/控制通道 ══════════
   桌面版：经 pywebview 的 api（js_api 桥）
   纯预览：直接用浏览器打开 _preview.html（file://，没有桥）

   ★★ 0.5.1 修的大坑：桌面版的页面也是 pywebview 内建服务器
      分发的（http://127.0.0.1:42001），protocol 同样是 http ——
      只按协议判断会把桌面版误当「网页版」，于是：
        · setShape() 以为没窗口可调，小浮窗只改界面不缩窗口
          （用户看到：内容缩了、窗口照旧、大片留白）
        · DOMContentLoaded 里把最小化/最大化/关闭按钮藏了
      唯一的铁证是窗口里有没有 pywebview 桥。桥是页面加载后才注入的
      （解析期拿不到），所以必须是运行时函数 —— 见下面的 isWebMode()。 */

/*  运行模式判定。
 *
 *  ★ 不能用 location.protocol 判断！桌面版也是 http：
 *    pywebview 把本地页面改写成 http://127.0.0.1:42001/ 再交给 WebView
 *    （见 pywebview 的 window.py / http.py），所以
 *    `location.protocol.startsWith('http')` 在**生产环境恒为 true**。
 *    以前 IS_WEB 就是这么算的，于是：
 *      · IS_WIDGET 恒为 false（全文件零引用，纯死值）；
 *      · 一旦桥注入失败，界面会切进「浏览器模式」，对着 /api/* 发请求 ——
 *        那个兜底服务（app/server.py）已经删除，这些请求全部落空，
 *        表现就是「窗口活着但没数据」，正是本项目最怕的形态。
 *
 *  现在唯一的判据是「窗口里有没有 pywebview 桥」：
 *    桥是页面加载后才注入的，所以必须是运行时函数，不能在解析期定型。
 *  另加一个「纯静态预览」判据（make_preview.py 生成的 _preview.html 用
 *  file:// 打开，且不会有桥）。 */
function isWebMode(){
  if(hasBridge()) return false;              /* 有桥 = 桌面版，铁证 */
  return location.protocol === 'file:';      /* file:// 且无桥 = 预览页 */
}
function hasBridge(){
  return !!(window.pywebview && window.pywebview.api);
}

/*  窗口按钮组（最小化/最大化/关闭）的可见性跟着「是不是真网页」走。
    桥注入得比 DOMContentLoaded 晚，所以：
      · DOMContentLoaded 时先按当前所知摆一次
      · pywebviewready 时再摆一次（桌面版到这一步才认得出自己）
    两处 DOMContentLoaded 都调它，别再各写一份了。 */
function syncWinbtnsVisibility(){
  const wb = document.querySelector('.winbtns');
  if(wb) wb.style.display = (isWebMode() && !hasBridge()) ? 'none' : '';
}
/*  ★ 事件挂在 window 上 —— pywebview 的 finish.js 是
    ``window.dispatchEvent(...)``，挂在 document 上根本收不到（踩过）。 */
window.addEventListener('pywebviewready', syncWinbtnsVisibility);
/*  兜底：万一事件先于脚本解析就发出去了（理论上不会），
    1.5 秒后再按「桥到底在不在」摆一次 —— 幂等，跑几次都没事。 */
setTimeout(syncWinbtnsVisibility, 1500);

function apiGetData(){
  if(window.pywebview?.api?.get_data) return window.pywebview.api.get_data();
  if(isWebMode()) return fetch('/api/data').then(r=>r.json());
  return Promise.resolve(null);
}
function apiRefresh(){
  if(window.pywebview?.api?.refresh) return window.pywebview.api.refresh();
  if(isWebMode()) return fetch('/api/refresh').then(r=>r.json());
}
function apiSetAuto(on){
  if(window.pywebview?.api?.set_auto_refresh) return window.pywebview.api.set_auto_refresh(on);
  if(isWebMode()) return fetch('/api/auto?on='+(on?'1':'0')).then(r=>r.json());
}
/* 预热状态：用于显示「开机已自动更新」徽章 */
function apiWarmStatus(){
  if(window.pywebview?.api?.warmup_status) return window.pywebview.api.warmup_status();
  if(isWebMode()) return fetch('/api/warmup').then(r=>r.json());
  return Promise.resolve(null);
}

/* ══════════ 首次运行引导（填账密即用）══════════ */
function apiSetupStatus(){
  if(window.pywebview?.api?.setup_status) return window.pywebview.api.setup_status();
  /* 网页模式：没有引导页（那是桌面版双击打开时的流程） */
  return Promise.resolve({need_setup:false});
}
function apiTestLogin(login, password, url){
  if(window.pywebview?.api?.test_login)
    return window.pywebview.api.test_login(login, password, url);
  return Promise.resolve({ok:false, error:'当前模式不支持测试登录'});
}
function apiSaveSetup(login, password, url, suser, spw, ecNames){
  if(window.pywebview?.api?.save_setup)
    return window.pywebview.api.save_setup(login, password, url, suser, spw,
                                           ecNames || '');
  return Promise.resolve({ok:false, error:'当前模式不支持保存'});
}
/*  单独存课表账号（引导页第 2 页）。
    ★ 不复用 save_setup —— 那时学校账号已经存好了，
      再调一次等于把它重写一遍（值虽然一样，但没必要多写一次盘，
      而且万一用户回头改了第一页又没重新提交就更麻烦）。 */
function apiSaveSchedule(login, password){
  if(window.pywebview?.api?.save_schedule)
    return window.pywebview.api.save_schedule(login, password);
  return Promise.resolve({ok:false, error:'当前模式不支持保存'});
}

/* ══════════ 开机自启 ══════════
   装/卸/查。「装好即用」的最后一环：开机后它在后台抓好数据，
   用户点开就有内容，不用等。 */
let AUTOSTART_ON = false;

function apiAutostartStatus(){
  if(window.pywebview?.api?.autostart_status)
    return window.pywebview.api.autostart_status();
  return Promise.resolve({installed:false, supported:false});
}
function apiAutostartInstall(){
  if(window.pywebview?.api?.autostart_install)
    return window.pywebview.api.autostart_install();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}
function apiAutostartUninstall(){
  if(window.pywebview?.api?.autostart_uninstall)
    return window.pywebview.api.autostart_uninstall();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

/* ══════════ 统计 ══════════ */
/* ══════════ GPA 页 ══════════
   「单开一页」而不是挤在顶部小卡里 —— 因为查分是高频动作，
   而且信息量大（整体 + 每门课 + 每门课的分类和最近几次）。
    折叠状态存在内存里，切页不丢；但刷新页面会重置（有意的，
     因为每次看分时想关注的课不一样，不想被上次的状态绑住）。 */
let gpaOpen = {};              /* { 课程名: true } */

/*  百分制 → 4.0 换算表（与后端 gpa.PERCENT_BANDS 一致：
    0.5.1 校准后 D 线是 65，65 以下一律 0.0）。

    ★ 单一来源：GPA 页那张说明表也由它生成（见 gpaNote）。
      0.5.1 时表格是手写的，算法改了表格没跟着改（还写着 63+/60+），
      用户照着表格自己算，跟显示的对不上。共用一份常量就不会再漂。 */
const GPA_BANDS = [
  [93, 4.0], [90, 3.7], [87, 3.3], [83, 3.0], [80, 2.7], [77, 2.3],
  [73, 2.0], [70, 1.7], [67, 1.3], [65, 1.0],
];

function estOf(p){
  if(p == null) return null;
  for(const [lo,g] of GPA_BANDS) if(p >= lo) return g;
  return 0.0;
}

/*  ══════════ 目标模拟（0.5.2）══════════
    「这门课想考到 X 分，剩下的作业得拿多少」

    类别权重和成绩都来自已经抓下来的数据，点一下就算，不再联网。
    算不了的时候（权重没读全 / 一个类别都没出分）
    直接说算不了 —— 不拿个假数字糊弄。
 */
function apiGoalSim(course, target){
  if(window.pywebview?.api?.goal_sim)
    return window.pywebview.api.goal_sim(course, target);
  if(isWebMode())
    return fetch('/api/goal_sim?course=' + encodeURIComponent(course)
      + '&target=' + encodeURIComponent(target)).then(r=>r.json());
  return Promise.resolve({ok:false, reason:'当前模式不支持'});
}

function goalSimHTML(r){
  if(!r || !r.ok){
    return `<span class="gsim-w">${esc((r && r.reason) || '算不了')}</span>`;
  }
  const t = (r.target != null) ? r.target.toFixed(1) : '?';
  const head = `<div class="gsim-h">目标 <b>${esc(t)}</b> 分 · `
    + `已达 <b>${(r.min || 0).toFixed(2)}</b>`
    + `（上限 ${(r.max || 0).toFixed(2)}）</div>`;

  let body = '';
  if(r.status === 'met'){
    body = `<div class="gsim-ok">已出分的部分就够目标了。</div>`;
  }else if(r.status === 'fixed'){
    body = `<div class="gsim-w">所有类别都已出分，成绩已经定了。</div>`;
  }else if(r.status === 'unreachable'){
    body = `<div class="gsim-w">剩下的全拿满分也只能到 `
      + `${(r.max || 0).toFixed(2)} 分。</div>`;
  }else if(r.status === 'possible'){
    body = `<div class="gsim-goal">剩下的类别加权平均要拿到 `
      + `<b>${(r.required || 0).toFixed(2)}</b> 分</div>`;
  }else{
    body = `<div class="gsim-w">${esc(r.reason || '')}</div>`;
  }

  let extra = '';
  if(r.unknown && r.unknown.length){
    extra = `<div class="gsim-u">还未出分的：`
      + r.unknown.map(u=>`${esc(u.name)}
          <span class="dim">(${esc(String(u.weight))}%)</span>`).join('、')
      + `</div>`;
  }
  const fx = r.formula ? `<div class="gsim-f">${esc(r.formula)}</div>` : '';
  const note = `<div class="gsim-n">已评分不代表该类别已结束；`
    + `这里是本机估算，不是学校官方成绩。</div>`;
  return head + body + extra + fx + note;
}


/* ══════════════════════════════════════════════════════════════
   成绩历史走势（0.5.2）

   ▸ 横轴单位固定为「日」，起止日期可选；可切成按周聚合
   ▸ 纵轴 4.0 制 GPA
   ▸ 可切到单科
   ▸ 真实快照与回算段用不同线型 —— 回算用的是作业截止日期，
     不是当时的真实记录，必须能一眼看出

   【为什么不画成标准 K 线】
     GPA 一天最多变一次，OHLC 四个值经常完全相同，
     画出来是一片「一字板」，信息量低于折线。
     这里用折线 + 变化点标记，纵轴与粒度同 K 线一致。
   ══════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════
   成绩走势（0.5.2）

   ▸ 用 KLineChart v10 画**真正的蜡烛图**（Apache-2.0，见 web/vendor/）
     参照 CampusDesk 的做法：OHLC + 均线 + 成交量 + 多时间粒度
   ▸ 每根蜡烛的含义（与行情图一致）：
        开 = 上一周期收盘（首根取周期内第一个值）
        高 / 低 = 周期内极值
        收 = 周期内最后一个值
        量 = 该周期新增的成绩条数
   ▸ 两种折算口径可切：
        「分段折算」按公开未加权 4.0 表逐档换算（与 GPA 页一致）
        「线性折算」用 分数÷100×4，曲线更平滑，但不是通行算法
   ▸ 真实快照与回算段：回算那一段是近似值，
     所以给一个「只看真实记录」开关，打开就只用有快照的点

   【为什么改用蜡烛图】
     起先是手绘的折线 SVG，观感与专业图表差距明显。
     CampusDesk 用的是 KLineChart 库 —— 同一个库能直接得到
     蜡烛、均线、十字光标、缩放的成熟效果，没必要自己重画。
   ══════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════
   成绩走势（0.5.2）

   形态：**逐日 GPA 的折线，用 K 柱呈现**。
     每根柱：开 = 前一日 GPA，收 = 当日 GPA，涨绿跌红。
     柱体本身表达「今天比昨天变了多少」，不做行情终端那一套。

   刻意去掉的东西（会让画面花眼）：
     · 均线（MA5/MA10）
     · 成交量副图
     · 最高/最低影线 —— 只留柱体
     · 十字光标上的价格标注、跟随光标的 OHLC 提示

   保留的交互只有一个：
     鼠标划到某根柱 → 弹卡片，写清「当天拿到哪几个成绩、开收各是多少、涨跌几 %」。

   另有一个「折叠持平日」开关：GPA 没变的日子不占位置（默认关闭）。

   【数据怎么来】
     首次打开：后端按截止日期把全部成绩排好，逐日算出 GPA，一次性生成序列
              并写入 data/gpa_history.json。
     之后：签名（成绩条数 / 最后截止日 / 快照范围）不变就直接读缓存；
           有新成绩则重算一次 —— 实测全量 16 毫秒、缓存命中 1 毫秒，
           没有必要做更复杂的增量。
   ══════════════════════════════════════════════════════════════ */
let HIST = {
  data: null, course: '', start: '', end: '',
  fold: false,               /* 折叠没有成绩的日子 */
  shape: 'candle',           /* candle = K 柱 | area = 折线 */
  chart: null, key: '', view: [],
};

const HIST_PALETTE = { up: '#4f8a70', down: '#c47768', flat: '#9aa3a0' };

function apiHistory(course, start, end, grain) {
  if (window.pywebview?.api?.history_series)
    return window.pywebview.api.history_series(course || '', start || '',
                                               end || '', grain || 'day');
  return Promise.resolve({ ok: false, error: '当前模式不支持', points: [] });
}

/*  逐日序列 → 柱体序列。
    range 固定按「日」，所以 index 就是天数；折叠后间距依然均匀，
    横轴日期由我们自己画（见 histAxis），不依赖库的时间轴。 */
function histBars(points, fold) {
  const rows = (points || [])
    .map(p => ({ p: p, ts: Date.parse(p.date + 'T00:00:00') }))
    .filter(r => Number.isFinite(r.ts))
    .sort((a, b) => a.ts - b.ts);
  const out = [];
  let prev = null;
  for (const r of rows) {
    const close = Number(r.p.gpa);
    if (!Number.isFinite(close)) continue;
    const open = prev == null ? close : prev;
    prev = close;
    /*  ★ 只折叠「当天没有任何成绩」的日子 ★
          现在的口径是「GPA 只由成绩算出来」，所以：
            · 有成绩的日子 → GPA 可能变、也可能恰好抵消，都要留着
            · 没成绩的日子 → GPA 数学上不可能变，折掉不影响信息
          这样折叠掉的一定是「什么都没发生」的日子，
          不会把「其实出了分」的那天藏起来。 */
    const hasGrades = !!(r.p.grades && r.p.grades.length);
    if (fold && out.length && !hasGrades) continue;
    out.push({
      p: r.p,
      open: open,
      close: close,
      first: out.length === 0,     /* 首日没有「前一天」可比 */
      changePct: out.length === 0 ? 0 : (open ? ((close - open) / open) * 100 : 0),
    });
  }
  return out;
}

function histCardHTML() {
  const d = HIST.data;
  const names = (d && d.course_names) || [];
  const nPts = (d && d.total) || 0;
  const opts = ['<option value="">全部课程（总览）</option>']
    .concat(names.map(n =>
      `<option value="${esc(n)}"${n === HIST.course ? ' selected' : ''}>${esc(n)}</option>`))
    .join('');

  return `<div class="sec histsec"><div class="sechead">
      <span class="t">${icon('i-chart','sm inl')}成绩走势</span>
      <span class="cnt">${nPts ? nPts + ' 天' : '—'}</span>
      <span class="flex"></span></div>
    <div class="secinner">
      <div class="histbar">
        <label class="histlb">从<input type="date" id="histStart" value="${esc(HIST.start)}"></label>
        <label class="histlb">到<input type="date" id="histEnd" value="${esc(HIST.end)}"></label>
        <select class="histsel" id="histCourse">${opts}</select>
        <button class="histrec ${HIST.fold ? 'on' : ''}" id="histFold"
          title="打开后，当天没有新成绩的日子不占位置（有成绩的一天一定保留）">
          <span class="dot"></span>折叠持平日</button>
        <div class="histseg" id="histShape">
          <button data-shape="candle" class="${HIST.shape === 'candle' ? 'on' : ''}">柱</button>
          <button data-shape="area" class="${HIST.shape === 'area' ? 'on' : ''}">折线</button>
        </div>
      </div>
      <div class="histchart" id="histWrap">
        <div class="histcv" id="histChart"></div>
        <div class="histaxis" id="histAxis"></div>
        <div class="histlegend">
          <span><i class="lg" style="background:${HIST_PALETTE.up}"></i>上升</span>
          <span><i class="lg" style="background:${HIST_PALETTE.down}"></i>下降</span>
          <span class="lghint">下沿 = 前一天，上沿 = 当天 · 悬停可看来源与当天成绩</span>
        </div>
        <!--  悬停卡片必须放在 .histchart 内部：
              它按 left/top 定位，而 .histchart 是 position:relative ——
              放在外面时它的定位原点是另一个元素，于是卡片飘到很远的地方。 -->
        <div class="histtip" id="histTip" hidden></div>
      </div>
      ${d && d.note ? `<div class="histnote">${esc(d.note)}</div>` : ''}
    </div></div>`;
}

function histDispose() {
  if (HIST.chart) {
    try { window.klinecharts.dispose(HIST.chart); } catch (_) {}
    HIST.chart = null;
    HIST.key = '';
  }
}

/*  横轴日期我们自己画：这样折叠后仍然均匀，不会因为跳日而出现空隙 */
function histAxisRender(bars) {
  const el = $('#histAxis');
  if (!el) return;
  if (!bars.length) { el.innerHTML = ''; return; }
  const picks = bars.length <= 4
    ? bars.map((b, i) => i)
    : [0, Math.floor((bars.length - 1) / 3),
       Math.floor((bars.length - 1) * 2 / 3), bars.length - 1];
  const uniq = [...new Set(picks)];
  el.innerHTML = uniq.map(i => {
    const left = bars.length === 1 ? 50 : (i / (bars.length - 1)) * 100;
    const md = bars[i].p.date.slice(5);
    let style = `left:${left}%`;
    if (i === 0) style += ';transform:translateX(0)';
    else if (i === uniq[uniq.length - 1]) style += ';transform:translateX(-100%)';
    else style += ';transform:translateX(-50%)';
    return `<span style="${style}">${esc(md)}</span>`;
  }).join('');
}

function histMount() {
  const box = $('#histChart');
  if (!box) return;
  const lib = window.klinecharts;
  if (!lib || typeof lib.init !== 'function') {
    box.innerHTML = `<div class="histempty">图表库没有加载。</div>`;
    return;
  }
  const d = HIST.data || {};
  const bars = histBars(d.points || [], HIST.fold);
  HIST.view = bars;
  histAxisRender(bars);

  if (!bars.length) {
    histDispose();
    box.innerHTML = `<div class="histempty">这段时间没有可画的数据。</div>`;
    return;
  }

  /*  用序号当时间戳：横轴由我们自己的 .histaxis 画，
      这样「折叠」不会在图上留下空洞。 */
  /*  钳制在 0~4：GPA 满分就是 4.0，任何超出的值都是算错了 */
  const clamp4 = (v) => Math.max(0, Math.min(4, Number(v) || 0));
  const rows = bars.map((b, i) => ({
    timestamp: (i + 1) * 86400000,
    /*  只留柱体：把高低收紧到开收之间，影线长度归零 */
    open: clamp4(b.open),
    high: Math.max(clamp4(b.open), clamp4(b.close)),
    low: Math.min(clamp4(b.open), clamp4(b.close)),
    close: clamp4(b.close),
    volume: 0,
  }));
  const maxVal = rows.reduce((m, r) => Math.max(m, r.high), 0);

  const key = JSON.stringify([rows.length, HIST.fold, HIST.shape,
                              box.clientWidth, rows[0] && rows[0].close,
                              rows.length && rows[rows.length - 1].close]);
  if (HIST.chart && HIST.key === key) { HIST.chart.resize(); return; }
  histDispose();
  box.innerHTML = '';

  const chart = lib.init(box, {
    layout: {
      barSpaceLimit: { min: 3, max: 26 },
      /*  ★ 纵轴不能超过 4.0 ★
          GPA 满分就是 4.0，但库默认在上方留 22% 空白，
          数据顶到 4.0 时算出来的刻度会出现 4.02 这种不存在的分数。
          所以贴近满分时把上方留白压成 0，让轴顶正好落在 4.00。 */
      yAxis: {
        scrollZoomEnabled: false,
        gap: { top: maxVal >= 3.999 ? 0 : 0.18, bottom: 0.16 },
      },
    },
    zoomAnchor: 'last_bar',
    locale: 'zh-CN',
  });
  if (!chart) { box.innerHTML = `<div class="histempty">图表初始化失败。</div>`; return; }
  HIST.chart = chart;
  HIST.key = key;

  chart.setZoomEnabled(false);
  chart.setSymbol({ ticker: 'GPA', pricePrecision: 2, volumePrecision: 0 });
  chart.setPeriod({ type: 'day', span: 1 });
  const isArea = HIST.shape === 'area';
  chart.setStyles({
    candle: {
      /*  柱：实心蜡烛（只留柱体）；折线：面积图 */
      type: isArea ? 'area' : 'candle_solid',
      /*  面积图样式（v10 用 area，旧版用 areaStyle，两个都给上） */
      area: {
        lineColor: '#4a7ec8', lineSize: 2, smooth: true,
        backgroundColor: 'rgba(74,126,200,.12)',
      },
      areaStyle: {
        lineColor: '#4a7ec8', lineSize: 2, smooth: true,
        backgroundColor: 'rgba(74,126,200,.12)',
      },
      bar: {
        upColor: HIST_PALETTE.up, downColor: HIST_PALETTE.down,
        noChangeColor: HIST_PALETTE.flat,
        upBorderColor: HIST_PALETTE.up, downBorderColor: HIST_PALETTE.down,
        noChangeBorderColor: HIST_PALETTE.flat,
        upWickColor: HIST_PALETTE.up, downWickColor: HIST_PALETTE.down,
        noChangeWickColor: HIST_PALETTE.flat,
      },
      /*  去掉最高/最低标注与最后一根的价格线 —— 只留柱子 */
      priceMark: { high: { show: false }, low: { show: false },
                   last: { show: false } },
      /*  去掉跟随光标的 OHLC 提示 */
      tooltip: { showRule: 'none' },
    },
    indicator: { tooltip: { showRule: 'none' } },
    /*  去掉十字光标（不再用鼠标位置显示 GPA） */
    crosshair: { show: false },
    /*  时间轴文字由我们自己画 */
    xAxis: { tickText: { show: false }, tickLine: { show: false } },
    grid: {
      show: true,
      horizontal: { show: true, color: 'rgba(150,150,150,.18)', size: 1,
                    style: 'dashed', dashedValue: [2, 3] },
      vertical: { show: false },
    },
  });

  /*  ★ 柱子必须铺满整个宽度 ★
      原来 barSpace 上限写死 26，柱子一少（比如折叠后剩 17 根）总宽就只有
      440px，而图宽约 1150px —— klinecharts 默认把数据**右对齐**，
      左侧于是空出一大块，看起来像「开头那段数据没了」。
      现在按可用宽度反算 barSpace，并把右侧空白距离设为 0。 */
  const plotW = Math.max(120, box.clientWidth - 58);   /* 减去右侧刻度宽度 */
  /*  ★ 柱宽封顶，剩余空间全部留在右边 → 整体靠左 ★

      两种难看的情况都要避开：
        · 柱子多时按宽度算会太细 → 下限 3px 保底
        · 柱子少时按宽度算会太宽（折叠后就剩十几根，能到 70px）→ 上限 26px
      封顶之后总宽不够铺满，就把**剩余空间全推到右侧**，
      于是数据从左边开始画 —— 时间序列本来就该从左往右读。 */
  const barSpace = Math.max(3, Math.min(26, plotW / Math.max(1, rows.length)));
  chart.setBarSpace(barSpace);
  const remainRight = Math.max(0, plotW - rows.length * barSpace);
  try { chart.setOffsetRightDistance(remainRight); } catch (_) {}
  try { chart.setMaxOffsetRightDistance(plotW); } catch (_) {}
  try { chart.setMaxOffsetLeftDistance(0); } catch (_) {}
  chart.setDataLoader({
    getBars: params => params.callback(
      params.type === 'init' ? rows : [], { backward: false, forward: false }),
  });
  chart.resize();
  histBindTip(box, chart);
  histBindWheel();
}

/*  悬停卡片：划到哪根柱，就说清那天的成绩与开收涨跌 */
function histBindTip(box, chart) {
  const tip = $('#histTip');
  if (!tip) return;
  box.onmousemove = (e) => {
    const r = box.getBoundingClientRect();
    let idx = -1;
    try {
      const res = chart.convertFromPixel(
        { x: e.clientX - r.left, y: e.clientY - r.top }, { paneId: 'candle_pane' });
      idx = res && typeof res.dataIndex === 'number' ? res.dataIndex : -1;
    } catch (_) { idx = -1; }
    const b = HIST.view[idx];
    if (!b) { tip.hidden = true; return; }

    const up = b.close > b.open + 1e-9;
    const down = b.close < b.open - 1e-9;
    const col = up ? HIST_PALETTE.up : (down ? HIST_PALETTE.down : HIST_PALETTE.flat);
    const arrow = up ? '▲' : (down ? '▼' : '—');
    const pct = Math.abs(b.changePct).toFixed(2);
    const gs = b.p.grades || [];
    const total = b.p.grades_total || gs.length;

    /*  两段来源不同，卡片上必须标清楚：
          记录 = 当天界面显示的数字；推算 = 由成绩倒推的近似值 */
    const isRec = b.p.source === 'record';
    const srcTag = isRec
      ? '<span class="htt-src rec">记录</span>'
      : '<span class="htt-src est">推算</span>';

    /*  首日没有「前一天」，写 0.00% 会让人以为「拿了成绩却没变化」 */
    let h = b.first
      ? `<div class="htt-top">
          <span class="htt-date">${esc(b.p.date)}</span>${srcTag}
          <span class="htt-pct" style="color:var(--muted)">首日</span>
        </div>
        <div class="htt-oc">收 <b style="color:${col}">${b.close.toFixed(2)}</b>
          <span class="htt-arw" style="opacity:.6">· 起点</span></div>`
      : `<div class="htt-top" style="color:${col}">
          <span class="htt-date">${esc(b.p.date)}</span>${srcTag}
          <span class="htt-pct">${arrow} ${pct}%</span>
        </div>
        <div class="htt-oc">开 <b>${b.open.toFixed(2)}</b>
          <span class="htt-arw">→</span> 收 <b style="color:${col}">${b.close.toFixed(2)}</b></div>`;

    if (gs.length) {
      h += `<div class="htt-gs">当天获得 ${total} 个成绩</div><ul class="htt-list">`;
      for (const g of gs) {
        h += `<li><span class="hg-n">${esc(g.name || '')}</span>`
          + `<span class="hg-s">${esc(g.score || '')}</span>`
          + `<span class="hg-c">${esc((g.course || '').slice(0, 16))}</span></li>`;
      }
      if (total > gs.length) h += `<li class="htt-more">…还有 ${total - gs.length} 个</li>`;
      h += `</ul>`;
    } else if (isRec) {
      /*  记录段：数值来自当天界面显示，与「有没有出分」没有必然对应 */
      h += `<div class="htt-none">当天没有新成绩；数值取当日实际记录</div>`;
    } else {
      /*  推算段：没有成绩，推算值不可能变 */
      h += `<div class="htt-none">当天没有新成绩，推算值不变</div>`;
    }

    tip.innerHTML = h;
    tip.hidden = false;
    /*  贴着光标，靠右就翻到左边 */
    const tw = tip.offsetWidth || 240;
    let x = e.clientX - r.left + 14;
    if (x + tw > r.width - 8) x = Math.max(4, e.clientX - r.left - tw - 14);
    tip.style.left = x + 'px';
    tip.style.top = Math.max(4, e.clientY - r.top + 12) + 'px';
  };
  box.onmouseleave = () => { tip.hidden = true; };
}

/*  ★ 图表内的滚轮必须滚页面，不能被图表吃掉 ★

    klinecharts 在容器上挂了 `wheel`（`{passive:false}` + preventDefault），
    鼠标停在图上就滚不动页面 —— 图比窗口还高，体验很差。

    【为什么以前那条拦截没生效】
      原来拦在 `#histWrap` 的捕获阶段，判据是
          Math.abs(deltaY) >= Math.abs(deltaX)
      触控板两指滚动时 deltaX / deltaY 经常接近，这条判据会漏判 ——
      漏了就等于没拦，事件照样落到图表上被 preventDefault。

    【现在的做法】
      在 **document** 上以捕获阶段拦：它比任何元素都早收到事件，
      target 落在图表内的**纯滚动**（未按 Ctrl/Shift/Alt）一律 stopPropagation。
      **不** preventDefault —— 页面默认滚动照常发生。
      按住 Ctrl（缩放）/ Shift（横向）时才放行，留给图表自己处理。

    ★ 用 document 而不是 #histWrap：图表重绘会换掉 #histWrap 的克隆，
      绑在元素上的监听容易漏；绑在 document 上只绑一次，永久有效。 */
function histBindWheel() {
  if (window.__histWheelBound) return;
  window.__histWheelBound = true;
  document.addEventListener('wheel', (e) => {
    const wrap = document.getElementById('histWrap');
    if (!wrap || !e.target || !wrap.contains(e.target)) return;
    //  Ctrl/⌘ = 缩放网页；Shift = 横向；这些不该被我们吞掉
    if (e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.stopPropagation();          /* 图表收不到，就不会 preventDefault */
  }, { capture: true, passive: false });
}

function loadHistory() {
  apiHistory(HIST.course, HIST.start, HIST.end, 'day').then(d => {
    /*  只用分段口径（4.0 档位）。曾经还有个「线性」口径，
        两种折算画出来肉眼几乎一样、还让人以为是切换坏了，已经去掉。 */
    HIST.data = d || { points: [], course_names: [] };
    const host = $('#page-gpa');
    const box = host ? host.querySelector('.histsec') : null;
    if (box && box.parentNode) {
      const tmp = document.createElement('div');
      tmp.innerHTML = histCardHTML();
      box.parentNode.replaceChild(tmp.firstElementChild, box);
    }
    histDispose();
    bindHistory();
    setTimeout(histMount, 30);
  }).catch(() => {});
}

function bindHistory() {
  const host = $('#page-gpa');
  if (!host) return;
  const s = host.querySelector('#histStart');
  const e = host.querySelector('#histEnd');
  const c = host.querySelector('#histCourse');
  if (s && !s.dataset.b) {
    s.dataset.b = '1';
    s.addEventListener('change', () => { HIST.start = s.value; loadHistory(); });
  }
  if (e && !e.dataset.b) {
    e.dataset.b = '1';
    e.addEventListener('change', () => { HIST.end = e.value; loadHistory(); });
  }
  if (c && !c.dataset.b) {
    c.dataset.b = '1';
    c.addEventListener('change', () => { HIST.course = c.value; loadHistory(); });
  }
  const fd = host.querySelector('#histFold');
  if (fd && !fd.dataset.b) {
    fd.dataset.b = '1';
    fd.addEventListener('click', () => {
      HIST.fold = !HIST.fold;
      fd.classList.toggle('on', HIST.fold);
      histMount();
    });
  }
  const sm = host.querySelector('#histShape');
  if (sm && !sm.dataset.b) {
    sm.dataset.b = '1';
    sm.querySelectorAll('[data-shape]').forEach(b => {
      b.addEventListener('click', () => {
        if (HIST.shape === b.dataset.shape) return;
        HIST.shape = b.dataset.shape;
        sm.querySelectorAll('[data-shape]').forEach(x =>
          x.classList.toggle('on', x === b));
        histMount();
      });
    });
  }
  window.addEventListener('resize', () => {
    if (HIST.chart) { try { HIST.chart.resize(); } catch (_) {} }
  }, { passive: true });
}

function ensureHistCard() {
  const host = $('#page-gpa');
  if (!host) return;
  if (host.querySelector('.histsec')) { bindHistory(); return; }
  const inner = host.querySelector('.secinner') || host;
  const tmp = document.createElement('div');
  tmp.innerHTML = histCardHTML();
  inner.parentNode.insertBefore(tmp.firstElementChild, inner);
  bindHistory();
}

function renderGpaPage(){
  const host = $('#page-gpa');
  if(!host) return;
  const s = (DATA && DATA.summary) || {};
  const courses = (DATA && DATA.courses) || [];

  if(!courses.length){
    host.innerHTML = `<div class="emptystate">
      <div class="es-ic">${icon('i-award','xxl')}</div>
      <div class="es-tt">还没有成绩数据</div>
      <div class="es-ds">${esc((DATA && DATA.message) || '正在读取课程……')}</div>
    </div>`;
    return;
  }

  const off = s.gpa_official;          /* {value, scale, label} 或 null */
  const est = s.gpa_estimated;         /* 数字或 null */
  const wEst = s.gpa_estimated_weighted;  /* 加权参考（AP/IB/HL +1.0，Honors/SL +0.5） */
  const lEst = s.gpa_estimated_linear;    /* 等比参考（÷100×4，仅供对照） */
  const mp  = s.mean_percent;
  const n   = s.gpa_estimated_count || 0;

  /*  兜底：后端 summary 里没有 GPA（刚抓完还没算）时，
     就**当场用课程行现算** —— 下面每行都有分数却显示「—」很奇怪。 */
  const _pcts = courses.map(c => c.overall_percent).filter(p => p != null && !isNaN(p));
  const useN  = n || _pcts.length;
  const useMp = (mp != null) ? mp
              : (_pcts.length ? _pcts.reduce((a,b)=>a+b,0)/_pcts.length : null);
  const useEst = (est != null) ? est
               : (_pcts.length
                  ? Math.round(_pcts.reduce((a,p)=>a+estOf(p),0)/_pcts.length*100)/100
                  : null);
  const useLEst = (lEst != null) ? lEst
               : (_pcts.length
                  ? Math.round(_pcts.reduce((a,p)=>a+Math.min(Math.max(p/25,0),4),0)/_pcts.length*100)/100
                  : null);

  /* —— 主卡：优先展示官方值 —— */
  const hasOff = !!(off && off.value != null);
  const bigVal = hasOff ? Number(off.value).toFixed(2)
                        : (useEst != null ? useEst.toFixed(2) : '—');
  const scale  = hasOff ? (off.scale || '4.00') : '4.00';
  const gc     = hasOff ? 'var(--green)'
                        : (useEst != null ? pctColor(useMp) : 'var(--dim)');

  let h = `<div class="ghero ${hasOff?'':'est'}" style="--gc:${gc}">
    <div class="gtag ${hasOff?'off':'es'}">
      ${hasOff ? '学校公布' : '本机估算'}</div>
    <div class="glabel">${icon('i-award','sm inl')}
      ${hasOff ? '学校 GPA' : 'GPA（估算）'}</div>
    <div class="gbig">${esc(bigVal)}<small>/ ${esc(scale)}</small></div>
    <div class="gsub">${hasOff
      ? `来自学校页面：<b>${esc(off.label || 'Cumulative GPA')}</b>。`
      : (useEst != null
          ? `按 <b>${useN} 门</b>有总评的课程<b>等权平均</b>换算，不是学校公布的官方值。`
          : '还没有任何一门课出总评，所以暂时算不出 GPA。')}</div>
    <div class="gmini">
      <div><b>${useMp!=null?useMp.toFixed(1)+'%':'—'}</b>平均分</div>
      <div><b>${useN}</b>计入门数</div>
      <div><b>${courses.length}</b>课程总数</div>
      ${wEst!=null?`<div title="难度加分参考：课程名带 AP/IB/HL +1.0，Honors/SL +0.5；学校不一定这么算">
        <b>${wEst.toFixed(2)}</b>加权参考</div>`:''}
      ${useLEst!=null?`<div title="等比参考：每门 分数÷100×4 再平均。不是学校通行算法，中低分会偏高，仅供对照">
        <b>${useLEst.toFixed(2)}</b>等比参考</div>`:''}
    </div>
  </div>`;

  /* —— 课程列表（有总评的排前面）—— */
  const sorted = [...courses].sort((a,b)=>{
    const ap = a.overall_percent, bp = b.overall_percent;
    if(ap == null && bp == null) return (a.name||'').localeCompare(b.name||'');
    if(ap == null) return 1;
    if(bp == null) return -1;
    return bp - ap;
  });

  h += `<div class="sec"><div class="sechead">`;
  h += `<span class="t">各科成绩</span>`;
  h += `<span class="cnt">${n} / ${courses.length} 门有总评</span>`;
  h += `<span class="flex"></span></div>`;
  h += `<div class="secinner">`;

  for(const c of sorted){
    const p = c.overall_percent;
    const lv = c.overall_level || '';
    const col = p != null ? pctColor(p) : lvlColor(lv);
    const key = c.name || '';
    const isOpen = !!gpaOpen[key];
    const estV = estOf(p);

    /* 明细：分类占比 + 最近几条成绩 */
    const cats = (c.categories || []).filter(x => x.percent != null);
    const graded = (c.tasks || [])
      .filter(t => t.graded && t.score && t.score !== 'N/A')
      .slice(0, 4);

    h += `<div class="grow ${isOpen?'open':''}" data-grow="${esc(key)}"
              style="--c:${col}" title="点击查看明细">
      <div class="gname">${esc(c.name || '未命名课程')}
        ${c.teacher ? `<i>${esc(c.teacher)}</i>` : ''}</div>
      ${lv ? `<span class="glv" style="--c:${col}">${esc(lv)}</span>`
           : `<span class="glv na">—</span>`}
      ${p != null
        ? `<span class="gpct" style="--c:${col}">${p.toFixed(1)}%</span>`
        : `<span class="gno">—</span>`}
      <span class="garw">▶</span>
    </div>
    <div class="gdetail ${isOpen?'open':''}" data-gdet="${esc(key)}">
      <div class="gdin">`;

    /* 估算 GPA 单科值 */
    if(estV != null){
      h += `<div class="gdtitle">${icon('i-award','sm inl')}单科估算</div>
        <div style="font-size:11.3px;color:var(--muted);margin-bottom:11px">
          按 ${p.toFixed(1)}% 换算约 <b style="color:var(--text)"
          >${estV.toFixed(2)}</b> / 4.0
          <span style="color:var(--dim)">（本机换算，非学校口径）</span>
        </div>`;
    }

    /* 分类占比 */
    if(cats.length){
      h += `<div class="gdtitle">${icon('i-chart','sm inl')}分类占比</div>`;
      for(const ct of cats.slice(0, 6)){
        const w = Math.max(2, Math.min(100, ct.percent));
        const cc = pctColor(ct.percent);
        h += `<div class="gcat" style="color:${cc}">
          <span class="cn">${esc(ct.name || '未命名分类')}
            ${ct.weight ? `<span style="color:var(--dim)"
              >· 权重 ${esc(String(ct.weight))}%</span>` : ''}</span>
          <span class="cbar"><i style="width:${w}%"></i></span>
          <span class="cv" style="color:${cc}">${ct.percent.toFixed(1)}%</span>
        </div>`;
      }
      /*  目标模拟（0.5.2）——“想考到 X 分，剩下的得拿多少”。
          只有读到类别权重时才显示入口（否则算了也是空的）。 */
      h += `<div class="gbody" id="gbody-${esc(key)}">
        <div class="gbodyrow">
          <input class="gin" type="number" min="0" max="100" step="0.5"
            id="gtg-${esc(key)}" placeholder="想考到多少分？">
          <button class="gbtn" data-gsim="${esc(key)}">算一下</button>
        </div>
        <div class="gsim" id="gsim-${esc(key)}"></div>
      </div>`;
    }

    /* 最近几次成绩 */
    if(graded.length){
      h += `<div class="gdtitle" style="margin-top:12px">
        ${icon('i-note','sm inl')}最近成绩</div>`;
      for(const t of graded){
        h += `<div class="gitem">
          <span class="gt">${esc(t.title || '')}</span>
          <span class="gs" style="color:${t.percent!=null?pctColor(t.percent):'var(--text)'}"
            >${esc(t.score)}${t.percent!=null
              ? ` <span style="color:var(--dim);font-weight:600"
                >${t.percent.toFixed(0)}%</span>` : ''}</span>
        </div>`;
      }
    }

    if(!cats.length && !graded.length){
      h += `<div class="gempty">这门课还没有可以展开的明细。</div>`;
    }

    /* 跳转原网页 */
    h += `<button class="fretry" style="margin-top:11px"
        data-gopen="${esc(c.url || '')}">
      ${icon('i-external','sm')}在 ManageBac 中打开</button>`;

    h += `</div></div>`;
  }
  h += `</div></div>`;

  /* —— 底部诚实说明 + 换算表 —— */
  h += gpaNote(hasOff);

  host.innerHTML = h;
  bindGpaPage();
  /*  成绩走势卡（0.5.2）：插在最上面，数据异步补上 */
  ensureHistCard();
  loadHistory();
}

/* 绑定 GPA 页的交互（展开 / 跳转） */
/* GPA 说明 —— 默认收起，只留一行小字 + 一个问号。
   「诚实声明」很重要（用户必须知道数字是不是官方的），
   但不必一屏大字压着 —— 收进折叠，想看的点一下。 */
function gpaNote(hasOff){
  return `<div class="gnote">
    <div class="gnote-head" data-gwhy="1">
      <span class="gnote-q">?</span>
      <span class="gnote-t">GPA 怎么来的</span>
      <span class="gnote-arw">▶</span>
    </div>
    <div class="gnote-body" data-gwhybody="1">
      <p>
        ${hasOff
          ? '来自学校页面的正式标注，以学校成绩单为准。'
          : '本机估算：每门课的总评按公开的未加权 4.0 标准表逐档换算，再等权平均。不是学校口径。'}
      </p>
      <p class="gnote-dim">未计入：学分、AP/IB 加权、重修、未出总评的课程。</p>
      <div class="gscale">
        ${GPA_BANDS.map(([lo,g]) =>
          `<span><b>${lo}+</b> → ${g.toFixed(1)}</span>`).join('')}
        <span><b>&lt;${GPA_BANDS[GPA_BANDS.length-1][0]}</b> → 0</span>
      </div>
    </div>
  </div>`;
}

function bindGpaPage(){
  const host = $('#page-gpa');
  if(!host) return;

  host.querySelectorAll('[data-grow]').forEach(row=>{
    if(row.dataset.gbound) return;
    row.dataset.gbound = '1';
    row.addEventListener('click', e=>{
      e.stopPropagation();
      const key = row.dataset.grow;
      const det = host.querySelector('[data-gdet="' + CSS.escape(key) + '"]');
      if(!det) return;
      const willOpen = !det.classList.contains('open');
      gpaOpen[key] = willOpen;
      row.classList.toggle('open', willOpen);
      det.classList.toggle('open', willOpen);

      /*  明细用 max-height 动画。高度必须**量真实值** ——
         写死一个大数字会让动画「前 90% 时间看不出变化、最后突然开完」。
         展开结束后解除限高（改成 none），免得内容变了被裁掉。 */
      if(willOpen){
        det.style.maxHeight = det.scrollHeight + 'px';
        setTimeout(()=>{
          if(det.classList.contains('open')) det.style.maxHeight = 'none';
        }, 430);
      }else{
        /* 收起：先把 none 拽回具体值，才动画得到 0 */
        det.style.maxHeight = det.scrollHeight + 'px';
        void det.offsetHeight;              /* 强制重排，坐实起点 */
        det.style.maxHeight = '';
      }
    });
  });

  host.querySelectorAll('[data-gopen]').forEach(b=>{
    if(b.dataset.gobound) return;
    b.dataset.gobound = '1';
    b.addEventListener('click', e=>{
      e.stopPropagation();
      openURL(b.dataset.gopen);
    });
  });

  /*  目标模拟（0.5.2）—— 「算一下」按钮与回车
      ★ 数据完全取自已抓到的类别权重，不额外联网。 */
  host.querySelectorAll('[data-gsim]').forEach(b=>{
    if(b.dataset.gsimbound) return;
    b.dataset.gsimbound = '1';
    const run = async (e)=>{
      e.stopPropagation();
      const key = b.dataset.gsim;
      const inp = document.getElementById('gtg-' + key);
      const box = document.getElementById('gsim-' + key);
      if(!inp || !box) return;
      const v = parseFloat(inp.value);
      if(!Number.isFinite(v)){
        box.innerHTML = `<span class="gsim-w">先填个目标分。</span>`;
        return;
      }
      box.innerHTML = `<span class="gsim-w">算着呢……</span>`;
      try{
        const r = await apiGoalSim(key, v);
        box.innerHTML = goalSimHTML(r);
      }catch(err){
        box.innerHTML = `<span class="gsim-w">算不了：${esc(String(err))}</span>`;
      }
    };
    b.addEventListener('click', run);
    const inp = document.getElementById('gtg-' + b.dataset.gsim);
    if(inp) inp.addEventListener('keydown', e=>{
      if(e.key === 'Enter'){ e.preventDefault(); run(e); }
    });
  });

  /* 「GPA 怎么来的」折叠：跟课程明细同一套量高动画 */
  const gh = host.querySelector('[data-gwhy]');
  const gb = host.querySelector('[data-gwhybody]');
  if(gh && gb && !gh.dataset.gwbound){
    gh.dataset.gwbound = '1';
    gh.addEventListener('click', e=>{
      e.stopPropagation();
      const willOpen = !gb.classList.contains('open');
      gh.classList.toggle('open', willOpen);
      gb.classList.toggle('open', willOpen);
      if(willOpen){
        gb.style.maxHeight = gb.scrollHeight + 'px';
        setTimeout(()=>{ if(gb.classList.contains('open'))
          gb.style.maxHeight = 'none'; }, 400);
      }else{
        gb.style.maxHeight = gb.scrollHeight + 'px';
        void gb.offsetHeight;
        gb.style.maxHeight = '';
      }
    });
  }
}

/* 单门课程一行（含可展开的明细） */
function renderStats(tasks){
  const t = tasks, s = DATA.summary||{};
  const todo = t.filter(x=>!x.completed);
  const overdue = todo.filter(x=>x.days_left!=null && x.days_left<0).length;
  const week = todo.filter(x=>x.days_left!=null && x.days_left>=0 && x.days_left<=7).length;
  const mp = s.mean_percent;

  /*  GPA 显示规则（借鉴 CampusDesk）：
       官方公布的优先 → 显示「GPA」并标注来自网站
       没有官方值 → 显示我们的估算，但**必须标明「估算」**
       两个都没有 → 显示平均分 */
  const off = s.gpa_official;          /* {value, scale, label} 或 null */
  const est = s.gpa_estimated;         /* 数字或 null */
  let gpaCell;
  if(off && off.value != null){
    gpaCell = {
      n: Number(off.value).toFixed(2),
      l: 'GPA',
      c: 'var(--green)',
      title: `网站公布的 ${off.label || 'GPA'}：${off.value} / ${off.scale}`
    };
  }else if(est != null){
    gpaCell = {
      n: est.toFixed(2),
      l: 'GPA（估算）',
      c: 'var(--blue)',
      title: `${s.gpa_estimated_count||0} 门有分数的课程等权估算，`
           + `非学校公布的官方值。把鼠标停在「平均分」上可看百分比。`
    };
  }else{
    gpaCell = {n:'—', l:'GPA', c:'var(--dim)', title:'还没有出分的课程'};
  }

  const st = [
    {n:todo.length, l:'待完成', c:'var(--blue)'},
    {n:overdue,  l:'已过期',   c:'var(--red)'},
    {n:week,     l:'7 天内',   c:'var(--orange)'},
    {n:t.length-todo.length, l:'已完成', c:'var(--green)'},
    {n:mp!=null?mp.toFixed(1)+'%':'—', l:'平均分', c:pctColor(mp),
     title: mp!=null ? `${s.graded_count||0} 门有总评的课程平均` : ''},
    gpaCell,
  ];
  /* ★ 同样要防重播：这段每秒执行，而 #stats .stat 上有入场动画。
     比对签名，内容没变就不碰 DOM。 */
  const stSig = JSON.stringify(st.map(x => [x.n, x.l, x.c]));
  if(stSig === lastStatsSig) return;
  lastStatsSig = stSig;

  $('#stats').innerHTML = st.map(x=>`
    <div class="stat" style="--c:${x.c}" ${x.title?`title="${esc(x.title)}"`:''}>
      <div class="n" style="color:${x.c}">${x.n}</div>
      <div class="l">${x.l}</div>
    </div>`).join('');
}

function isQuiz(t){
  const s = ((t.title || '') + ' ' + (t.kind || '')).toLowerCase();
  return /quiz|测验|小测|test\b/.test(s);
}
function isHomework(t){
  const s = ((t.kind || '') + ' ' + (t.title || '')).toLowerCase();
  return /homework|作业|hw\b/.test(s);
}

/* ══════════ 任务卡片 ══════════
   opts.showMark = 是否显示「我已完成」按钮。
    用户要求：没有紧急待办时自动隐藏，避免界面多余干扰。
     （已经标记过的卡片永远显示「撤销」，否则会没法反悔） */
function taskCards(list, opts){
  if(!list.length) return `<div class="empty">暂无</div>`;
  const o = opts || {};
  const showMark = o.showMark !== false;

  return `<div class="grid">${list.map(t=>{
    const d = t.days_left;
    /* 已完成 → 绿色；否则按剩余天数着色 */
    const col = t.completed ? 'var(--green)'
      : d==null?'var(--blue)': d<0?'var(--red)': d<=1?'var(--red)':
        d<=3?'var(--orange)': d<=7?'var(--yellow)':'var(--green)';
    const cid = t.class_id || '', tid = t.class_id ? (t.url.match(/core_tasks\/(\d+)/)||[])[1] : '';

    /*  状态标签（我已完成 / 已提交 / 已评分）——
        ★ 它**不放**在底部的 .tags 里，而是单独渲染到卡片右上角。
        原因：底部标签行动态增减（截止日 / 分类 / 类型 / 分数…），
        状态标签混在里面会被挤来挤去，长标题时还容易和
        「未提交」「截止 xxx」撞在一起，看着很乱。
        它是个**状态**，不是「标签之一」—— 放右上角最合适。 */
    const doneTag = t.manual
      ? `<span class="st" title="你自己标记的完成，只在本机生效">${icon('i-check','sm')}我已完成</span>`
      : (t.submitted ? `<span class="st">${icon('i-check','sm')}已提交</span>`
         : (t.graded ? `<span class="st">${icon('i-check','sm')}已评分</span>`
            : `<span class="st">${icon('i-check','sm')}已完成</span>`));

    /* 按钮：未完成 → 显示「我已完成」（受 showMark 控制）
             已完成且是手动标记 → 显示「撤销」（永远显示） */
    let mkBtn = '';
    if(t.completed){
      if(t.manual) mkBtn = `<button class="mkbtn undo"
        title="撤销标记，恢复为未完成" data-unmk="1"
        >${icon('i-undo','sm')}撤销</button>`;
    }else if(showMark){
      mkBtn = `<button class="mkbtn"
        title="线下已交纸质版 / 老师未给分？标记后不再提示"
        data-mk="1">${icon('i-check','sm')}我已完成</button>`;
    }

    /* 下载键：**只给有附件的任务显示**（用户要求）
       —— 避免每个卡片都挂一个用不上的 ⤓ */
    const hasDl = !!t.has_attachment && !!tid;

    return `<div class="card${t.manual?' manual-marked':''}${t.completed?' isdone':''}" style="--c:${col}" data-url="${esc(t.url)}">
      <div class="cardacts">
        ${t.completed ? doneTag : ''}
        ${hasDl?`<button class="dlbtn" title="下载本任务附件"
          data-dl="${esc(cid)}|${esc(tid)}|${esc(t.title)}">${icon('i-download','sm')}</button>`:''}
        ${mkBtn}
      </div>
      ${t.course?`<div class="cname"><span class="dot"></span>${esc(t.course)}</div>`:''}
      <div class="h">${esc(t.title)}</div>
      <div class="tags">
        ${t.completed ? '' : dueTag(d)}
        ${t.due_date?`<span class="tag dim">截止 ${esc(t.due_date)}</span>`:''}
        ${t.category?`<span class="tag cat">${esc(t.category)}</span>`:''}
        ${t.kind?`<span class="tag">${esc(t.kind)}</span>`:''}
        ${t.graded&&t.score&&t.score!=='N/A'
          ? `<span class="tag grade">${esc(t.score)}</span>` :''}
        ${(!t.completed && t.pending)?'<span class="tag dim">未提交</span>':''}
      </div>
    </div>`;
  }).join('')}</div>`;
}

/* ══════════ 课程块 ══════════ */
function courseBlock(c, idx){
  const col = colorOf(idx);
  const open = openCourses[c.class_id];
  const lv = c.overall_level, pc = c.overall_percent;
  const cats = (c.categories||[]).filter(x=>x.name.toLowerCase()!=='overall');

  /* 分类加权条 */
  const catRows = cats.map(x=>{
    const p = x.percent;
    const w = p!=null ? Math.min(100,p) : 0;
    return `<div class="catrow">
      <span class="catname" title="${esc(x.name)}${x.weight!=null?' ('+x.weight+'%)':''}">
        ${esc(x.name)}${x.weight!=null?` <span style="color:var(--dim)">${x.weight}%</span>`:''}
      </span>
      <span class="catbar"><i style="width:${w}%;background:${x.graded?pctColor(p):'var(--dim)'}"></i></span>
      <span class="catval" style="color:${x.graded?pctColor(p):'var(--dim)'}">
        ${x.graded?(x.level?esc(x.level)+' ':'')+(p!=null?p.toFixed(1)+'%':''):'未出分'}
      </span>
    </div>`;
  }).join('');

  /* 任务明细表 */
  const rows = (c.tasks||[]).map(t=>{
    const g = t.score && t.score!=='N/A';
    let st, stc;
    if(t.submitted){ st='已提交'; stc='ok'; }
    else if(t.late){ st='迟交'; stc='due'; }
    else if(t.pending){ st='待提交'; stc='soon'; }
    else if(g){ st='已评分'; stc='ok'; }
    else if(t.days_left!=null && t.days_left<0){ st='已过期'; stc='due'; }
    else { st='—'; stc='dim'; }
    return `<tr data-url="${esc(t.url)}">
      <td class="nm" title="${esc(t.title)}">${esc(t.title)}</td>
      <td style="color:var(--dim);white-space:nowrap">${esc(t.due_date||'—')}</td>
      <td>${t.kind?`<span class="tag">${esc(t.kind)}</span>`:''}</td>
      <td class="sc" style="color:${g?(t.level?lvlColor(t.level):pctColor(t.percent)):'var(--dim)'}">
        ${g?esc(t.score):'—'}
      </td>
      <td><span class="tag ${stc}">${st}</span></td>
    </tr>`;
  }).join('');

  /* 成绩环 */
  const R=34, C=2*Math.PI*R;
  const frac = pc!=null ? Math.min(1,Math.max(0,pc/100)) : 0;
  const ring = `<svg width="92" height="92" viewBox="0 0 92 92">
    <circle cx="46" cy="46" r="${R}" fill="none" stroke="#ede5d6" stroke-width="9"/>
    <circle cx="46" cy="46" r="${R}" fill="none" stroke="${pc!=null?pctColor(pc):'#ddd3bf'}"
      stroke-width="9" stroke-linecap="round" stroke-dasharray="${C}"
      stroke-dashoffset="${C}" transform="rotate(-90 46 46)">
      <animate attributeName="stroke-dashoffset" from="${C}"
        to="${C*(1-frac)}" dur=".85s" fill="freeze" calcMode="spline"
        keySplines="0.22 1 0.36 1" keyTimes="0;1"/>
    </circle>
    <text x="46" y="44" text-anchor="middle" fill="${lv?lvlColor(lv):'var(--dim)'}"
      font-size="22" font-weight="800">${lv?esc(lv):'—'}</text>
    <text x="46" y="60" text-anchor="middle" fill="#a89e8b" font-size="9">
      ${pc!=null?pc.toFixed(2)+'%':'未出分'}</text>
  </svg>`;

  return `<div class="course ${open?'open':''}" style="--c:${col}"
      data-course="${esc(c.class_id)}">
    <div class="chead" data-ch="${esc(c.class_id)}">
      <span class="cdot"></span>
      <span class="cname2">${esc(c.name)}</span>
      <span class="cmini">
        ${c.pending_count?`<span class="tag soon">${c.pending_count} 待交</span>`:''}
        ${c.late_count?`<span class="tag due">${c.late_count} 迟交</span>`:''}
        ${lv?`<span class="tag grade" style="color:${lvlColor(lv)}">${esc(lv)}
             ${pc!=null?'· '+pc.toFixed(1)+'%':''}</span>`
            :`<span class="tag dim">无成绩</span>`}
        <span class="tag dim">${(c.tasks||[]).length}</span>
      </span>
    </div>
    <div class="cbody">
      <div class="gradehero">
        ${ring}
        <div style="flex:1">
          <div style="font-size:11px;font-weight:700;margin-bottom:6px">
            ${c.teacher?`教师：${esc(c.teacher)}`:'分类权重成绩'}
          </div>
          ${catRows||'<div class="empty" style="padding:6px">暂无分类成绩</div>'}
        </div>
      </div>
      ${(c.tasks||[]).length?`<table class="tbl">
        <thead><tr><th>任务</th><th>截止</th><th>类型</th><th>成绩</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody></table>`:`<div class="empty">该课程暂无任务</div>`}
      <div class="tags" style="margin-top:9px">
        <span class="tag ok">已交 ${c.submitted_count||0}</span>
        ${c.late_count?`<span class="tag due">迟交 ${c.late_count}</span>`:''}
        ${c.pending_count?`<span class="tag soon">待交 ${c.pending_count}</span>`:''}
        <button class="fbtn" id="fb-${esc(c.class_id)}"
          data-files="${esc(c.class_id)}|${esc(c.name)}">${icon('i-folder','sm')}资料</button>
        <button class="fbtn" id="df-${esc(c.class_id)}"
          data-dlall="${esc(c.class_id)}|${esc(c.name)}">${icon('i-download','sm')}全部下载</button>
      </div>
    </div>
  </div>`;
}

/* ══════════ 图表 ══════════ */
/* ══════════ 课表页 ══════════ */
function hm(iso){
  /* 取时分。兼容两种来源：
       · 抓取的课   "2026-09-18T08:30"       （T 分隔）
       · 节次表     "2026-09-20 08:00:00"    （空格 + 秒）
      只认 T 的话，节次表的时间会全部显示成空 —— 实测踩过。 */
  if(!iso) return '';
  const m = /(\d{2}:\d{2})(?::\d{2})?\s*$/.exec(String(iso).trim());
  if(m) return m[1];
  const m2 = /[T ](\d{2}:\d{2})/.exec(iso);
  return m2 ? m2[1] : '';
}
function minsUntil(iso){
  if(!iso) return null;
  /*  同时认 T 分隔和空格分隔（节次表用空格 + 秒） */
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso);
  if(!m) return null;
  const t = new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5]);
  return Math.round((t - new Date()) / 60000);
}
function countdownText(mins){
  if(mins==null) return '';
  if(mins < 0) return '已结束';
  if(mins === 0) return '即将开始';
  if(mins < 60) return `${mins} 分钟后`;
  const h = Math.floor(mins/60), r = mins%60;
  return r ? `${h} 小时 ${r} 分后` : `${h} 小时后`;
}

/* 跨天的课用「相对时间」更好懂：
   今天  → 「3 小时后上课」（精确到分）
   明天  → 「明天 08:00 上课」
   更远  → 「周一 08:00 上课」/「9月21日 08:00 上课」
   距离超过 2 天时，精确到分钟毫无意义，反而误导。 */
const WEEK_CN = ['周日','周一','周二','周三','周四','周五','周六'];

function whenText(lesson, mins){
  if(mins == null) return '';
  const today = todayISO();
  if(lesson.day === today){
    if(mins < 0) return '已结束';
    return `${countdownText(mins)}上课`;
  }

  const tmr = tomorrowISO();
  const t = hm(lesson.start);
  if(lesson.day === tmr) return `明天 ${t} 上课`;

  const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(lesson.day || '');
  if(!dm) return `${countdownText(mins)}上课`;

  const d = new Date(+dm[1], +dm[2]-1, +dm[3]);
  const now = new Date();
  const days = Math.round((d - new Date(now.getFullYear(), now.getMonth(),
                                         now.getDate())) / 86400000);
  if(days === 2) return `后天 ${t} 上课`;
  if(days < 7)   return `${WEEK_CN[d.getDay()]} ${t} 上课`;
  return `${+dm[2]}月${+dm[3]}日 ${t} 上课`;
}

/* 卡片左上角的日期提示（今天不显示） */
function dayHint(day){
  if(!day || day === todayISO()) return '';
  if(day === tomorrowISO()) return '明天';
  const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if(!dm) return day;
  const d = new Date(+dm[1], +dm[2]-1, +dm[3]);
  const now = new Date();
  const days = Math.round((d - new Date(now.getFullYear(), now.getMonth(),
                                         now.getDate())) / 86400000);
  if(days === 2) return '后天';
  if(days < 7) return WEEK_CN[d.getDay()];
  return `${+dm[2]}月${+dm[3]}日`;
}

/* ══════════ 下一节课查找（跨天） ══════════
   关键：不能只看今天 —— 今天课全上完后仍应显示「明天第一节」。
   返回 {lesson, mins, running, dayLabel} 或 null。 */
function findNextLesson(lessons){
  if(!lessons || !lessons.length) return null;

  /* 正在上的课优先 */
  const running = lessons.find(l=>{
    const a = minsUntil(l.start), b = minsUntil(l.end);
    return a!=null && b!=null && a<=0 && b>0;
  });
  if(running) return {lesson: running, mins: minsUntil(running.start),
                      running: true};

  /* 最近的未来课（不限今天） */
  let best = null, bestM = null;
  for(const l of lessons){
    const m = minsUntil(l.start);
    if(m==null || m<=0) continue;
    if(bestM==null || m<bestM){ best = l; bestM = m; }
  }
  if(!best) return null;
  return {lesson: best, mins: bestM, running: false};
}

function tomorrowISO(){
  const d = new Date(); d.setDate(d.getDate()+1);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

/* 课表行：单行紧凑布局（时间 | 课程 | 教室 | 状态） */
function lessonCard(l, opts){
  const o = opts || {};
  const mins = minsUntil(l.start);
  const endM = minsUntil(l.end);
  const running = mins!=null && mins<=0 && endM!=null && endM>0;
  const past = endM!=null && endM<=0;
  /* 「即将开始」只对今天且 30 分钟内的课生效（未来的课不该闪） */
  const soon = !running && !past && mins!=null && mins<=30
               && l.day === todayISO();

  const col = running ? 'var(--green)'
    : past ? 'var(--dim)'
    : soon ? 'var(--orange)'
    : 'var(--blue)';

  /*  空档（没课的节次，标成「自习」）和自编课要区分出来：
       · 自习  → 不给倒计时/进行中徽章（它不是真课），颜色也淡
       · 自编  → 紫色 + 「自编」小标签，一眼能认出是自己加的
      自编课的 subject 里可能带了前缀 [上操] 之类，这里拆出来当标签用。 */
  const isSelf = !!l.selfStudy;
  const isCustom = !!l.custom;

  let badge = '';
  if(!isSelf){
    if(running) badge = '<span class="sb">进行中</span>';
    else if(!past && soon) badge = `<span class="sb">${countdownText(mins)}</span>`;
  }

  const cls = 'srow'
    + (running && !isSelf ? ' now' : '')
    + (soon && !isSelf ? ' soon' : '')
    + (past ? ' past' : '')
    + (isSelf ? ' selfstudy' : '')
    + (isCustom ? ' custom' : '');

  /* 自编课的标签：subject 形如 "[上操] 周一集会" */
  let title = l.subject || '(未命名)';
  let flag = '';
  const fm = /^\[([^\]]+)\]\s*(.*)$/.exec(title);
  if(isCustom && fm){
    flag = `<span class="cflag">${esc(fm[1])}</span>`;
    title = fm[2] || fm[1];
  } else if(isCustom){
    flag = '<span class="cflag">自编</span>';
  }

  /* 自习不显示时间范围里的破折号（没意义，反而占位） */
  const timeTxt = isSelf
    ? esc(hm(l.start))
    : (esc(hm(l.start)) + (l.end ? '–' + esc(hm(l.end)) : ''));

  return `<div class="${cls}" style="--c:${col}">
    <span class="st">${timeTxt}</span>
    <span class="sj">${esc(title)}</span>
    ${flag}
    <span class="sq">${esc(l.room||'')}${l.teacher?(l.room?' · ':'')+esc(l.teacher):''}</span>
    ${badge}
  </div>`;
}

/* ══════════════════════════════════════════════════════════════════
   外观设置（圆角 / 透明度 / 毛玻璃）

   三个值都存 localStorage，改完立刻生效（只改变量，不重绘 DOM）。
   ══════════════════════════════════════════════════════════════════ */

const LOOK_KEY = 'mb.look';

/* 圆角三档 —— 每档是一整套（成体系才好看，只改一个会很怪） */
const RADIUS_PRESETS = {
  small: { r: '12px',  lg: '15px',  xl: '18px',  sm: '8px',  xs: '5px'  },
  mid:   { r: '20px',  lg: '24px',  xl: '28px',  sm: '11px', xs: '7px'  },
  large: { r: '28px',  lg: '33px',  xl: '38px',  sm: '15px', xs: '10px' },
};

/* 透明度基准（默认档） */
const ALPHA_BASE = 72;

function loadLook(){
  let d = {};
  try{ d = JSON.parse(localStorage.getItem(LOOK_KEY) || '{}') || {}; }
  catch(e){ d = {}; }

  /* 低端机默认关模糊（省显卡） */
  const weak = (navigator.hardwareConcurrency || 4) <= 4;
  return {
    radius: d.radius || 'mid',
    alpha: (typeof d.alpha === 'number') ? d.alpha : ALPHA_BASE,
    blur: (typeof d.blur === 'boolean') ? d.blur : !weak,
  };
}

function saveLook(o){
  try{ localStorage.setItem(LOOK_KEY, JSON.stringify(o)); }catch(e){}
}

/* 把设置应用到 CSS 变量上 */
function applyLook(o){
  const rt = document.documentElement;

  /* ① 圆角 */
  const rp = RADIUS_PRESETS[o.radius] || RADIUS_PRESETS.mid;
  rt.style.setProperty('--r',    rp.r);
  rt.style.setProperty('--r-lg', rp.lg);
  rt.style.setProperty('--r-xl', rp.xl);
  rt.style.setProperty('--r-sm', rp.sm);
  rt.style.setProperty('--r-xs', rp.xs);

  /* ② 透明度：按比例缩放三个层级
      不透明度越高 → alpha 越大；限制在 0.45~1.0，
       太低会导致文字看不清（背景透过来太花）。

      ★ 用 --tintRGB 而不是写死「255,253,248」——
        换配色（比如浅绿系 / 深色）时玻璃底要跟着变，
        写死的话卡片永远是米白的，一眼看出没配套。
        --tintRGB 由 applyTheme() 写入。 */
  const tint = (getComputedStyle(rt).getPropertyValue('--tintRGB')
                || '255,253,248').trim() || '255,253,248';
  const k = Math.max(0.45, Math.min(1, o.alpha / 100));
  const f = v => Math.max(0.45, Math.min(1, v * k / (ALPHA_BASE / 100)));
  rt.style.setProperty('--glass',   `rgba(${tint},${f(0.72).toFixed(3)})`);
  rt.style.setProperty('--glass-2', `rgba(${tint},${f(0.55).toFixed(3)})`);
  rt.style.setProperty('--glass-3', `rgba(${tint},${f(0.86).toFixed(3)})`);

  /* ③ 毛玻璃
      关掉时要把背景变成**不透明实色** —— 只去掉 blur 会留一个
       半透明的糊状层，比开着还难看。 */
  if(o.blur){
    rt.style.setProperty('--glassBlur', 'saturate(1.7) blur(22px)');
  }else{
    rt.style.setProperty('--glassBlur', 'none');
    rt.style.setProperty('--glass',   `rgba(${tint},0.985)`);
    rt.style.setProperty('--glass-2', `rgba(${tint},0.97)`);
    rt.style.setProperty('--glass-3', `rgba(${tint},1)`);
  }
}

let LOOK = null;


/* ══════════ 浏览器选择（0.5.2）══════════
   之前写死 Edge，且只能手改 data/browser.json —— 一般用户根本不知道有这个文件。
   现在设置面板里直接选：自动 / Edge / Chrome / Brave / Chromium，
   并且可以打开「复用日常浏览器的登录」，省掉重新登录。 */

let BROWSER_CUR = 'auto';
let BROWSER_REUSE = false;

function apiBrowserGet(){
  if(window.pywebview?.api?.browser_get)
    return window.pywebview.api.browser_get();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}
function apiBrowserSet(browser, reuse){
  if(window.pywebview?.api?.browser_set)
    return window.pywebview.api.browser_set(browser, !!reuse);
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}


/* ══════════ 诊断报告（0.5.2）══════════
   崩溃时后端会自动写一份报告（app/crashlog.py）。
   这里给用户两个入口：手动生成一份、打开报告文件夹。
   报告内容可以直接贴进 GitHub issue，自带系统环境与出错位置。 */

function apiCrashStatus(){
  if(window.pywebview?.api?.crash_status)
    return window.pywebview.api.crash_status();
  return Promise.resolve({ok:false});
}
function apiCrashMake(){
  if(window.pywebview?.api?.crash_make)
    return window.pywebview.api.crash_make();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}
function apiCrashOpen(){
  if(window.pywebview?.api?.crash_open)
    return window.pywebview.api.crash_open();
  return Promise.resolve({ok:false});
}

function loadCrashSettings(){
  const row = $('#setCrashRow');
  if(!row) return;
  if(!(window.pywebview?.api?.crash_status)){ row.hidden = true; return; }
  row.hidden = false;

  const paint = (st)=>{
    const val = $('#setCrashVal');
    const desc = $('#setCrashDesc');
    if(val){
      val.textContent = (st && st.latest_name)
        ? st.latest_name : '还没有报告';
    }
    if(desc){
      if(st && st.previous_crash){
        desc.textContent = '上次运行是异常退出，报告已保存' +
          (st.previous_at ? '（' + st.previous_at + '）' : '');
      }else{
        desc.textContent = '程序异常退出时会自动保存一份报告' +
          '（已去掉账号密码等敏感信息）';
      }
    }
  };

  apiCrashStatus().then(paint).catch(()=>{});

  const mk = $('#setCrashMake');
  if(mk && !mk.dataset.b){
    mk.dataset.b = '1';
    mk.addEventListener('click', ()=>{
      mk.disabled = true;
      const old = mk.textContent;
      mk.textContent = '正在生成…';
      apiCrashMake().then(r=>{
        mk.disabled = false;
        mk.textContent = old;
        if(r && r.ok){
          toast('已生成：' + r.name);
          loadCrashSettings();
        }else{
          toast('生成失败：' + ((r && r.error) || '未知原因'), 'err');
        }
      }).catch(()=>{
        mk.disabled = false;
        mk.textContent = old;
        toast('生成失败', 'err');
      });
    });
  }

  const op = $('#setCrashOpen');
  if(op && !op.dataset.b){
    op.dataset.b = '1';
    op.addEventListener('click', ()=>{
      apiCrashOpen().then(r=>{
        if(!r || !r.ok) toast('打不开文件夹', 'err');
      }).catch(()=>toast('打不开文件夹', 'err'));
    });
  }
}


function loadBrowserSettings(){
  const row = $('#setBrowserRow');
  if(!row) return;
  apiBrowserGet().then(st=>{
    if(!st || !st.ok){ row.hidden = true; return; }
    const inst = st.installed || [];
    if(!inst.length){ row.hidden = true; return; }
    row.hidden = false;

    BROWSER_CUR = st.browser || 'auto';
    BROWSER_REUSE = !!st.reuse;

    const items = [{key:'auto', name:'自动'}]
      .concat(inst.map(x=>({key:x.key, name:x.name})));
    const pick = $('#setBrowserPick');
    if(pick){
      pick.innerHTML = items.map(it=>
        '<button data-bk="' + esc(it.key) + '"' +
        (it.key === BROWSER_CUR ? ' class="on"' : '') + '>' +
        esc(it.name) + '</button>').join('');
      pick.querySelectorAll('[data-bk]').forEach(btn=>{
        btn.addEventListener('click', ()=>{
          const k = btn.getAttribute('data-bk') || 'auto';
          apiBrowserSet(k, BROWSER_REUSE).then(r=>{
            if(r && r.ok){
              BROWSER_CUR = k;
              toast('已切换浏览器：' + (k === 'auto' ? '自动' : k));
              loadBrowserSettings();
            }else{
              toast('保存失败：' + ((r && r.error) || '未知原因'), 'err');
            }
          }).catch(()=>toast('保存失败', 'err'));
        });
      });
    }

    const val = $('#setBrowserVal');
    if(val){
      if(BROWSER_CUR === 'auto'){
        val.textContent = '自动' + (st.active ? '（当前 ' + st.active + '）' : '');
      }else{
        const hit = items.find(i=>i.key === BROWSER_CUR);
        val.textContent = hit ? hit.name : BROWSER_CUR;
      }
    }
    const desc = $('#setBrowserDesc');
    if(desc){
      desc.textContent = st.active_path
        ? ('当前使用：' + st.active + ' · ' + st.active_path)
        : '没检测到可用的 Chromium 浏览器（Edge / Chrome / Brave / Chromium 装一个即可）';
    }

    const rw = $('#setReuse');
    if(rw){
      rw.classList.toggle('on', BROWSER_REUSE);
      const rd = $('#setReuseDesc');
      if(rd){
        if(BROWSER_REUSE && st.reuse_note){
          /*  后端会告诉我们「这次能不能真复用」——
              浏览器开着的时候只能退回独立 profile，
              这件事必须让用户看见，否则他会以为开关没生效 */
          rd.textContent = st.reuse_note;
        }else{
          rd.textContent = BROWSER_REUSE
            ? '直接用你平时那个浏览器里的登录态（那个浏览器要先退出）'
            : '开启后直接用你平时那个浏览器里的登录态，不用再登一次';
        }
      }
      rw.onclick = ()=>{
        BROWSER_REUSE = !BROWSER_REUSE;
        apiBrowserSet(BROWSER_CUR, BROWSER_REUSE).then(r=>{
          if(r && r.ok){
            rw.classList.toggle('on', BROWSER_REUSE);
            if(rd){
              rd.textContent = BROWSER_REUSE
                ? '直接用你平时那个浏览器里的登录态（那个浏览器要先退出）'
                : '开启后直接用你平时那个浏览器里的登录态，不用再登一次';
            }
            toast(BROWSER_REUSE ? '会复用日常浏览器的登录态'
                                : '已改为使用独立数据目录');
          }else{
            BROWSER_REUSE = !BROWSER_REUSE;      /* 失败就回退 */
            toast('保存失败：' + ((r && r.error) || '未知原因'), 'err');
          }
        }).catch(()=>{
          BROWSER_REUSE = !BROWSER_REUSE;
          toast('保存失败', 'err');
        });
      };
    }
  }).catch(()=>{ row.hidden = true; });
}


function openSettings(){
  LOOK = LOOK || loadLook();
  const host = $('#setpanel');
  if(!host) return;

  /* 同步控件到当前值 */
  host.querySelectorAll('#setR span').forEach(el=>{
    el.classList.toggle('on', el.dataset.r === LOOK.radius);
  });
  const ra = $('#setA');
  if(ra) ra.value = String(LOOK.alpha);
  const bl = $('#setBlur');
  if(bl) bl.classList.toggle('on', !!LOOK.blur);
  setLookLabels();

  /*  开机自启的真实状态在系统里，不在 localStorage ——
      异步问一次后端，问到之前先按上次已知状态显示。 */
  const au = $('#setAuto');
  if(au){
    au.classList.toggle('on', AUTOSTART_ON);
    apiAutostartStatus().then(st=>{
      if(!st || !st.supported){
        /* 网页版 / 非 Windows：这项没意义，整行藏掉 */
        const row = $('#setAutoRow');
        if(row) row.hidden = true;
        return;
      }
      AUTOSTART_ON = !!st.installed;
      au.classList.toggle('on', AUTOSTART_ON);
      const desc = $('#setAutoDesc');
      if(desc){
        desc.textContent = AUTOSTART_ON
          ? '开机会在后台抓好数据，打开即用'
          : '已关闭。打开小组件时才会去取数据';
      }
    }).catch(()=>{});
  }

  /*  浏览器选择：桌面版才有意义 */
  try{ loadBrowserSettings(); }catch(e){}
  /*  诊断报告入口 */
  try{ loadCrashSettings(); }catch(e){}

  host.classList.add('open');
}

function closeSettings(){
  $('#setpanel')?.classList.remove('open');
}

/* ══════════════════════════════════════════════════════════════
   搜索（0.5.2）

   【为什么搜索结果要强制刷新成绩】
     老师登完分还会改分。搜索这个动作的意思就是「这条作业现在到底多少分」，
     拿上一次抓取的缓存回答，等于给出一个已经被改掉的旧分数。
     所以每次搜索都让后端按需重抓：指定科目只重抓那一门（2–3 秒），
     没指定就重抓全部（10–15 秒）。
     连续输入时改用 force=false 复用已有数据 —— 否则每敲一个字母
     就往站点打一轮请求，很容易触发限速。

   【排版为什么像 GPA 页】
     结果行直接复用 .grow / .gdetail，与 GPA 页展开科目是同一套样式、
     同一段 max-height 动画，展开手感一致。
   ══════════════════════════════════════════════════════════════ */
let srBusy = false;        /* 同一时刻只跑一个搜索请求 */
let srPending = null;      /* 忙碌期间攒下的最新一次请求 */
let srTimer = null;        /* 输入防抖 */
let srLastRefresh = 0;     /* 上次真的重抓过的时间戳 */

function apiSearch(query, course, force){
  if(window.pywebview?.api?.search)
    return window.pywebview.api.search(query || '', course || '', !!force);
  return Promise.resolve({ok:false, error:'当前模式不支持搜索'});
}

function openSearch(){
  const host = $('#searchpanel');
  if(!host) return;
  fillSearchCourses();
  host.classList.add('open');
  const inp = $('#srInput');
  if(inp){ inp.focus(); inp.select(); }
  runSearch(true);            /* 打开即强制刷新一次 */
}

function closeSearch(){ $('#searchpanel')?.classList.remove('open'); }

function fillSearchCourses(){
  const sel = $('#srCourse');
  if(!sel) return;
  const cur = sel.value;
  const names = ((DATA && DATA.courses) || [])
    .map(c => c && c.name).filter(Boolean);
  sel.innerHTML = ['<option value="">全部科目</option>']
    .concat(names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`))
    .join('');
  sel.value = names.indexOf(cur) >= 0 ? cur : '';
}

function runSearch(force){
  if(srBusy){ srPending = !!force; return; }   /* 串行化：别并发打站点 */
  srBusy = true;
  const query = ($('#srInput')?.value || '').trim();
  const course = ($('#srCourse')?.value || '').trim();
  const stat = $('#srStat');
  if(stat && force){
    stat.innerHTML = `${icon('i-refresh','sm inl')} 正在重新读取`
      + `${course ? '该科目' : '全部课程'}的成绩……`;
  }
  apiSearch(query, course, force).then(r=>{
    srBusy = false;
    if(force) srLastRefresh = Date.now();
    if(!r || !r.ok){ renderSearchFail(r); }
    else { renderSearchResults(r); }
    if(srPending !== null){
      const f = srPending; srPending = null;
      runSearch(f);
    }
  }).catch(e=>{
    srBusy = false;
    renderSearchFail({error:String(e)});
    if(srPending !== null){ const f = srPending; srPending = null; runSearch(f); }
  });
}

function renderSearchFail(r){
  const stat = $('#srStat'), res = $('#srRes');
  const msg = (r && r.error) ? esc(r.error) : '未知原因';
  if(stat) stat.innerHTML = `<span class="warn">搜索没有完成：${msg}</span>`;
  if(res) res.innerHTML = '';
}

/* 分组标题上的小圆点颜色：取该组已出分任务的平均分 */
function srGroupColor(g){
  const ps = (g.tasks || []).map(t => t.percent).filter(v => v != null);
  if(!ps.length) return 'var(--blue)';
  return pctColor(ps.reduce((a,b)=>a+b,0) / ps.length);
}

/* 折叠行标题下面那行小字：分类 · 类型 · 截止 */
function srSubTitle(t){
  const bits = [];
  if(t.category) bits.push(t.category);
  if(t.kind && t.kind !== t.category) bits.push(t.kind);
  if(t.due_date) bits.push('截止 ' + t.due_date);
  return bits.join(' · ');
}

/* 展开后的「全部信息」 */
function srDetailHTML(t){
  const p = t.percent;
  const cc = p != null ? pctColor(p) : 'var(--text)';
  let h = '';
  if(t.score){
    h += `<div class="srscore" style="color:${cc}">${esc(t.score)}`
      + (p != null ? `<small>${p.toFixed(1)}%</small>` : '') + `</div>`;
  }else if(t.graded){
    h += `<div class="srscore" style="color:var(--dim)">已评分</div>`;
  }else{
    h += `<div class="srscore" style="color:var(--dim)">尚未出分</div>`;
  }
  const kv = [];
  if(t.course) kv.push(['课程', t.course]);
  if(t.category) kv.push(['分类', t.category]);
  if(t.kind) kv.push(['类型', t.kind]);
  if(t.due_date) kv.push(['截止', t.due_date + (t.due_text ? '（' + t.due_text + '）' : '')]);
  if(t.publish_date) kv.push(['布置', t.publish_date]);
  if(t.status) kv.push(['提交', t.status]);
  if(t.has_attachment) kv.push(['附件', '有']);
  if(kv.length){
    h += `<div class="srmeta">`
      + kv.map(kv1 => `<span class="kv">${esc(kv1[0])} <b>${esc(kv1[1])}</b></span>`).join('')
      + `</div>`;
  }
  if(t.description){
    const d = String(t.description).trim();
    if(d){
      h += `<div class="srdesc">${esc(d.slice(0, 420))}`
        + (d.length > 420 ? '…' : '') + `</div>`;
    }
  }
  if(t.url){
    h += `<button class="fretry" data-sropen="${esc(t.url)}">`
      + `${icon('i-external','sm')}在 ManageBac 中打开</button>`;
  }
  return h;
}

function renderSearchResults(r){
  const stat = $('#srStat'), res = $('#srRes');
  const now = new Date();
  const hhmmss = [now.getHours(), now.getMinutes(), now.getSeconds()]
    .map(n => String(n).padStart(2,'0')).join(':');
  const rf = r.refresh || {};
  const bits = [`命中 <b>${r.total || 0}</b> 条 · <b>${r.courses_total || 0}</b> 门课程`];
  if(!rf.skipped){
    if(rf.ok === false){
      bits.push(`<span class="warn">成绩未能刷新：${esc(rf.error || '未知原因')}</span>`);
    }else{
      bits.push(`成绩已重新读取（${((rf.took_ms || 0) / 1000).toFixed(1)} 秒）`);
    }
  }
  bits.push(`检索于 ${hhmmss}`);
  let html = bits.join(' · ');
  if(rf.cooldown){
    html += `<br><span class="warn">站点限速中，约 ${rf.cooldown} 秒内可能读不全。</span>`;
  }
  const chg = rf.changed || [];
  if(chg.length){
    const items = chg.slice(0,6).map(x =>
      `${esc(x.course)} · ${esc(x.title)} → <b>${esc(x.score || '—')}</b>`);
    html += `<div class="srchg"><b>刚读取到 ${chg.length} 条分数有变化的记录</b><br>`
      + items.join('<br>') + (chg.length > 6 ? '<br>…' : '') + `</div>`;
  }
  if(stat) stat.innerHTML = html;
  if(!res) return;

  if(!r.total){
    res.innerHTML = `<div class="srnone">没有找到匹配的作业。<br>`
      + `<b>可以换个关键词，或把科目切到「全部科目」。</b></div>`;
    return;
  }

  let h = '';
  let gi = 0;
  for(const g of (r.groups || [])){
    const col = srGroupColor(g);
    h += `<div class="srgroup">`;
    h += `<div class="srgh" style="--c:${col}"><span class="cdot"></span>`
      + `${esc(g.course || '未命名课程')}`
      + `<span class="srgn">${g.count || (g.tasks || []).length} 项</span></div>`;
    for(const t of (g.tasks || [])){
      const k = 'sr' + (gi++);
      const p = t.percent;
      const lv = t.level || '';
      const cc = p != null ? pctColor(p) : lvlColor(lv);
      const sub = srSubTitle(t);
      h += `<div class="grow" data-srow="${k}" style="--c:${cc}" title="点击展开全部信息">`
        + `<div class="gname">${esc(t.title || '未命名')}`
        + (sub ? `<i>${esc(sub)}</i>` : '') + `</div>`
        + (lv ? `<span class="glv" style="--c:${cc}">${esc(lv)}</span>`
              : `<span class="glv na">—</span>`)
        + (p != null ? `<span class="gpct" style="--c:${cc}">${p.toFixed(1)}%</span>`
                     : `<span class="gno">—</span>`)
        + `<span class="garw">▶</span>`
        + `</div>`
        + `<div class="gdetail" data-sdet="${k}"><div class="gdin">`
        + srDetailHTML(t) + `</div></div>`;
    }
    h += `</div>`;
  }
  res.innerHTML = h;
  bindSearchRows();
}

function bindSearchRows(){
  const host = $('#srRes');
  if(!host) return;
  /*  展开动画与 GPA 页完全一致：
      max-height 必须量**真实高度**，写死一个大数字会让动画
      「前 90% 时间看不出变化、最后突然开完」。 */
  host.querySelectorAll('[data-srow]').forEach(row=>{
    row.addEventListener('click', e=>{
      e.stopPropagation();
      const k = row.dataset.srow;
      const det = host.querySelector('[data-sdet="' + k + '"]');
      if(!det) return;
      const willOpen = !det.classList.contains('open');
      row.classList.toggle('open', willOpen);
      det.classList.toggle('open', willOpen);
      if(willOpen){
        det.style.maxHeight = det.scrollHeight + 'px';
        setTimeout(()=>{
          if(det.classList.contains('open')) det.style.maxHeight = 'none';
        }, 430);
      }else{
        det.style.maxHeight = det.scrollHeight + 'px';
        void det.offsetHeight;
        det.style.maxHeight = '';
      }
    });
  });
  host.querySelectorAll('[data-sropen]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      openURL(btn.dataset.sropen);
    });
  });
}

function bindSearch(){
  /*  幂等：页面里有两个 DOMContentLoaded 监听器都会调到这里 */
  if(bindSearch._done) return;
  bindSearch._done = true;

  $('#btnSearch')?.addEventListener('click', e=>{
    e.stopPropagation();
    if($('#searchpanel')?.classList.contains('open')) closeSearch();
    else openSearch();
  });
  $('#srClose')?.addEventListener('click', closeSearch);
  $('#searchpanel')?.addEventListener('click', e=>{
    if(e.target && e.target.id === 'searchpanel') closeSearch();
  });

  const inp = $('#srInput');
  if(inp){
    inp.addEventListener('input', ()=>{
      clearTimeout(srTimer);
      srTimer = setTimeout(()=>{
        /*  距上次强制刷新超过 15 秒才再抓一次，否则用已有数据本地筛 */
        runSearch(Date.now() - srLastRefresh > 15000);
      }, 420);
    });
    inp.addEventListener('keydown', e=>{
      if(e.key === 'Enter'){ clearTimeout(srTimer); runSearch(true); }
      if(e.key === 'Escape'){ closeSearch(); }
    });
  }
  /*  换科目 = 换一门课的成绩，必须重抓 */
  $('#srCourse')?.addEventListener('change', ()=> runSearch(true));
}

function setLookLabels(){
  const vr = $('#setValR');
  if(vr){
    vr.textContent = ({small:'小', mid:'中', large:'大'})[LOOK.radius] || '';
  }
  const va = $('#setValA');
  if(va) va.textContent = LOOK.alpha + '%';
}

function bindSettings(){
  /*  幂等：只绑一次。
      页面里有两个 DOMContentLoaded 监听器都会调到这里，
      重复绑定会让部分逻辑互相干扰（重建 DOM 会丢监听器）。
      踩过的坑：表现为「点了没反应」且不报错。 */
  if(bindSettings._done) return;
  bindSettings._done = true;

  /* 圆角：三档 */
  document.querySelectorAll('#setR span').forEach(el=>{
    if(el.dataset.sbound) return;
    el.dataset.sbound = '1';
    el.addEventListener('click', e=>{
      e.stopPropagation();
      LOOK = LOOK || loadLook();
      LOOK.radius = el.dataset.r;
      document.querySelectorAll('#setR span').forEach(x=>
        x.classList.toggle('on', x === el));
      applyLook(LOOK); saveLook(LOOK); setLookLabels();
    });
  });

  /* 透明度滑块 */
  const ra = $('#setA');
  if(ra && !ra.dataset.sbound){
    ra.dataset.sbound = '1';
    ra.addEventListener('input', ()=>{
      LOOK = LOOK || loadLook();
      LOOK.alpha = +ra.value;
      applyLook(LOOK); saveLook(LOOK); setLookLabels();
    });
  }

  /* 毛玻璃开关 */
  const bl = $('#setBlur');
  if(bl && !bl.dataset.sbound){
    bl.dataset.sbound = '1';
    bl.addEventListener('click', e=>{
      e.stopPropagation();
      LOOK = LOOK || loadLook();
      LOOK.blur = !LOOK.blur;
      bl.classList.toggle('on', LOOK.blur);
      applyLook(LOOK); saveLook(LOOK);
    });
  }

  /* 开机自动更新开关 —— 这项不存 localStorage，
     真实状态在系统里（启动文件夹有没有那个快捷方式），
     所以每次点都要问一遍后端。 */
  const au = $('#setAuto');
  if(au && !au.dataset.sbound){
    au.dataset.sbound = '1';
    au.addEventListener('click', async e=>{
      e.stopPropagation();
      const desc = $('#setAutoDesc');
      au.disabled = true;
      const want = !AUTOSTART_ON;
      try{
        const r = want ? await apiAutostartInstall() : await apiAutostartUninstall();
        if(r && r.ok){
          AUTOSTART_ON = want;
          au.classList.toggle('on', want);
          if(desc) desc.textContent = want
            ? '开机会在后台抓好数据，打开即用'
            : '已关闭。打开小组件时才会去取数据';
          toast(want ? '已开启开机自动更新' : '已关闭开机自动更新', 'ok', '');
        }else{
          if(desc) desc.textContent = '没能改成功：'
            + ((r && r.error) || '未知原因');
          toast('没能改成功', 'err', '');
        }
      }catch(err){
        if(desc) desc.textContent = '没能改成功：' + err;
      }finally{
        au.disabled = false;
      }
    });
  }

  /* 恢复默认 */
  const rs = $('#setReset');
  if(rs && !rs.dataset.sbound){
    rs.dataset.sbound = '1';
    rs.addEventListener('click', e=>{
      e.stopPropagation();
      const weak = (navigator.hardwareConcurrency || 4) <= 4;
      LOOK = { radius:'mid', alpha: ALPHA_BASE, blur: !weak };
      applyLook(LOOK); saveLook(LOOK);
      closeSettings(); openSettings();
      toast('已恢复默认外观', 'ok', '');
    });
  }

  /* 开关面板 */
  $('#btnSettings')?.addEventListener('click', e=>{
    e.stopPropagation();
    if($('#setpanel')?.classList.contains('open')) closeSettings();
    else openSettings();
  });
  $('#setClose')?.addEventListener('click', closeSettings);
  $('#setpanel')?.addEventListener('click', e=>{
    if(e.target && e.target.id === 'setpanel') closeSettings();
  });

  /* 初始化：页面一打开就应用（不闪默认样式） */
  LOOK = loadLook();
  applyLook(LOOK);
}

/* ══════════════════════════════════════════════════════════════════
   Teams / EC 面板

   EC = English Corner，每天会在 Teams 频道里发 PDF（名单 / 安排）。
   这里把消息和 PDF 文字集中到一处，方便查。

   ★ 交互
     · 「打开 Teams」→ 弹出浏览器窗口，你登录（只需一次）
     · 「同步」     → 往回翻几屏，读消息，分类存在本机
     · 「取附件」   → 下载 EC 的 PDF 并提取文字
     · 搜索框       → 在 EC 文字里全文搜索，命中的词会高亮
   ══════════════════════════════════════════════════════════════════ */

let TM_ITEMS = [];
let TM_KEY = '';
/*  Teams 的状态快照（有没有同步过、多少条）。
    标签栏的徽章要用它区分「没同步过」和「同步过但没消息」。 */
let TM_STATUS_CACHE = {};

function tmEsc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* 高亮搜索词（先转义再做标记，避免 XSS） */
function tmHilite(text, kw){
  const safe = tmEsc(text);
  if(!kw) return safe;
  try{
    const re = new RegExp('(' + kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    return safe.replace(re, '<mark>$1</mark>');
  }catch(e){ return safe; }
}

/* ══════════ 反应徽章 ══════════
   后端把 Teams 的反应读成了 [{emoji:'👍',n:3}, ...]
   （以前是一句「3 个赞表情符号。 3」混在正文里，莫名其妙）。

   抽成函数是因为**两处**都要渲染：弹窗视图和整页视图。
   而且回复里也有一层 —— 写成内联模板会套到三层，
   又难读又容易漏个反引号。

   small=true 用在回复里（那里空间窄，整体缩一号）。 */
function tmReactions(list, small){
  const arr = list || [];
  if(!arr.length) return '';
  const cls = small ? 'tm-reas sm' : 'tm-reas';
  const one = r => `<span class="tm-rea"><i>${tmEsc(String(r.emoji||''))}</i
    ><b>${tmEsc(String(r.n == null ? 1 : r.n))}</b></span>`;
  return `<div class="${cls}">${arr.map(one).join('')}</div>`;
}

function tmSetStatus(html, ok){
  const bar = $('#tmBar'), txt = $('#tmStatus');
  if(bar) bar.classList.toggle('on', !!ok);
  if(txt) txt.innerHTML = html;
  /*  ★ 整页那一版的状态栏也要跟着 —— 两个视图共用同一份数据，
      只更新弹窗的话，切到整页会看到过时的「还没同步过」。 */
  tmPageStatus(html, ok);
}

function tmRender(){
  const box = $('#tmList');
  if(!box) return;

  const kw = TM_KEY.trim();
  let items = TM_ITEMS;
  if(kw){
    const low = kw.toLowerCase();
    /*  搜索范围：标题 / 正文 / PDF 文字 / 频道 / 人名 / 附件名 / 日期。
       人名一定要能搜 —— EC 的核心用法就是查「我在不在名单上」。 */
    items = items.filter(it => {
      const atts = (it.attachments || []).map(a => a.name || '').join(' ');
      const blob = (it.title||'') + ' ' + (it.text||'') + ' ' +
                   (it.file_text||'') + ' ' + (it.channel||'') + ' ' +
                   (it.author||'') + ' ' + (it.date||'') + ' ' + atts;
      return blob.toLowerCase().indexOf(low) >= 0;
    });
  }

  if(!items.length){
    box.innerHTML = kw
      ? `<div class="tm-empty">没有匹配「${tmEsc(kw)}」的内容</div>`
      : `<div class="tm-empty">
           <span class="oi">${icon('i-teams','xxl')}</span>
           还没有数据。<br>先点「打开 Teams」登录，再点「同步」。
         </div>`;
    return;
  }

  /* 按日期分组（新的在前） */
  const byDate = {};
  items.forEach(it=>{
    const d = it.date || '未标注日期';
    (byDate[d] = byDate[d] || []).push(it);
  });
  const dates = Object.keys(byDate).sort().reverse();

  let h = '';
  dates.forEach(d=>{
    const list = byDate[d];
    h += `<div class="tm-sec">${tmEsc(d)}
      <span class="tm-n">${list.length}</span></div>`;
    list.forEach((it, i)=>{
      const isEc = it.kind === 'ec';
      const txt = (it.text || '').slice(0, 600);
      const atts = it.attachments || [];
      const hasFile = !!(it.file_text || '').trim();

      h += `<div class="tm-item" data-tmi="${d}|${i}">
        <div class="tm-h">
          <span class="tm-t">${tmHilite(it.title || '消息', kw)}</span>
          <span class="tm-d">${tmEsc((it.timeLabel||'').slice(0,18))}</span>
        </div>
        ${txt ? `<div class="tm-p">${tmHilite(txt, kw)}</div>` : ''}
        ${tmReactions(it.reactions)}
        <div class="tm-m">
          ${isEc ? '<span class="tm-tag">EC</span>' : ''}
          ${it.author ? `<span class="tm-tag">${tmHilite(it.author, kw)}</span>` : ''}
          ${atts.length ? `<span class="tm-tag att">附件 ${atts.length}</span>` : ''}
          ${hasFile ? '<span class="tm-tag pdf">已读 PDF</span>' : ''}
        </div>
        ${hasFile ? `<div class="tm-file">
          ${it.file_note ? `<span class="tm-note">${tmEsc(it.file_note)}</span>` : ''}
          ${tmHilite(it.file_text.slice(0, 8000), kw)}
        </div>` : ''}
      </div>`;
    });
  });

  box.innerHTML = h;

  /* 点击展开/收起 */
  box.querySelectorAll('[data-tmi]').forEach(el=>{
    el.addEventListener('click', e=>{
      if(e.target.tagName === 'MARK') return;
      el.classList.toggle('open');
    });
  });
}

async function tmLoad(keepStatus){
  let st = {}, items = [], stats = {};
  try{
    st = await apiTeamsStatus();
    /*  全量抓取的结果优先 —— 它覆盖所有团队的所有频道。
        旧的那份（单频道）只在没抓过全量时兜底。 */
    const r = await apiTmAll('', '', '');
    items = (r && r.items) || [];
    stats = await apiTmStats();
  }catch(e){}

  TM_ITEMS = items;
  TM_STATUS_CACHE = st || {};
  TM_STATS = stats || {};
  TMP_STATUS_AT = (stats && stats.at) || '';

  /*  keepStatus=true 时只刷新列表，不动状态栏 ——
      否则刚同步完的结果会被「上次同步 …」立刻盖掉。 */
  if(!keepStatus){
    const nT = (stats && stats.nTeams) || 0;
    const nC = (stats && stats.nChannels) || 0;
    const at = ((stats && stats.at) || '').slice(5, 16);
    if(items.length){
      tmSetStatus(
        `已收 <b>${nT}</b> 个团队 · <b>${nC}</b> 个频道 · ` +
        `<b>${items.length}</b> 条消息` +
        (at ? ` · ${tmEsc(at)}` : ''), true);
    }else if(st && st.ready){
      tmSetStatus(
        `上次同步 <b>${tmEsc((st.updated||'').slice(5,16))}</b>` +
        ` · EC <b>${st.ec_count}</b> 条 · 其他 <b>${st.post_count}</b> 条`, true);
    }else{
      tmSetStatus(st && st.note ? tmEsc(st.note) : '还没同步过');
    }
  }
  tmRender();
  tmRenderPage();      /*  顶级标签页那一版也要跟着刷 ——
                          两个视图共用 TM_ITEMS，但布局与交互不同 */

  /* 顺手把标签栏的计数刷一下（徽章的更新在 render 里，
     但那条路径依赖 DATA 刷新，这里直接补一次） */
  try{
    const btm = $('#badge-tm');
    if(btm && items.length){
      btm.textContent = items.length > 99 ? '99+' : items.length;
      btm.title = `已收 ${items.length} 条消息`;
    }
  }catch(e){}
}

/* ══════════════════════════════════════════════════════════════════
   Teams 整页（顶级标签）

   与弹窗版的区别：
     · 整页布局，信息密度更高（弹窗只有 430px 宽）
     · 多一排「频道」筛选胶囊 —— 能只看某个会话
     · 状态栏与按钮固定在顶部，滚动时不动
     · 消息按「频道 → 日期」两级分组（弹窗版只按日期）

   为什么要有整页：Teams 里的消息不止 EC 一类 ——
   作业通知、老师反馈、班级群消息都在里面。
   弹窗的定位是「快速查 EC」，整页才是「看全部消息」。

   ★ 「同步」与「全部收一遍」的区别：
       同步       = 只读当前打开的频道（快，几秒）
       全部收一遍 = 遍历所有团队的所有频道（慢，约 3 分钟），
                    但一次就把整个 Teams 收全，之后不用再管
   ══════════════════════════════════════════════════════════════════ */

let TM_CH = '';          /* 频道筛选：空 = 全部 */
let TM_TEAM = '';        /* 团队筛选：空 = 全部团队（两级导航的第一级） */
let TMP_STATUS_AT = '';  /* 这批缓存是什么时候抓的（用来提示「太旧了」） */
let TM_PAGE_KEY = '';    /* 整页的搜索词（与弹窗各自独立） */
let TM_STATS = {};       /* 全量抓取的概览（团队/频道/条数/时间） */
/*  Teams 主内容的分区：posts（帖子）/ files（文件） */
let TM_VIEW = 'posts';

let TM_CRAWL_TIMER = null;

/*  ★ 停止正在进行的 Teams 抓取 ★
   深度归档最长要跑 90 分钟，以前点下去就停不下来，
   只能等它自己结束或者杀掉程序。后端提供了取消标志，
   这里给它一个按钮入口（运行中再点一次原按钮即可）。 */
function apiTmCrawlCancel(){
  if(window.pywebview?.api?.tm_crawl_cancel)
    return window.pywebview.api.tm_crawl_cancel();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

/* 把所有频道整理出来（去重、排序、带条数）。
   同一个频道名可能在多个团队里出现，所以用「团队 / 频道」做键，
   但显示上只写频道名（够认了）。 */
/* 团队清单（团队名 → 消息数），按条数降序 —— 两级导航的第一级 */
function tmTeams(){
  const m = {};
  (TM_ITEMS || []).forEach(it=>{
    const t = (it.team || '').trim() || '未标注团队';
    m[t] = (m[t] || 0) + 1;
  });
  return Object.entries(m).sort((a,b)=> b[1] - a[1]);
}

function tmChannels(){
  const m = {};
  (TM_ITEMS || []).forEach(it=>{
    const c = (it.channel || '').trim() || '未标注频道';
    const t = (it.team || '').trim();
    const key = t ? `${t} / ${c}` : c;
    m[key] = (m[key] || 0) + 1;
  });
  return Object.entries(m).sort((a,b)=> b[1] - a[1]);
}

/* 频道胶囊上显示的短名（团队太多时只留频道名，省地方） */
function tmChanShort(key){
  const p = key.split(' / ');
  return p.length > 1 ? p[1] : key;
}

function tmRenderPage(){
  /*  ★ 结构模仿 Teams ★
        左栏图标 → 团队列 → 频道列 → 主内容（帖子 / 文件）
      业务逻辑沿用原来的：搜索、卡片、附件下载、回复、归档按钮全保留。 */
  const box = document.getElementById('page-tm');
  if(!box) return;

  const kw = TM_PAGE_KEY.trim();
  const ch = TM_CH;

  let items = (TM_ITEMS || []).slice();

  /* 团队筛选（第一级） */
  if(TM_TEAM){
    items = items.filter(it => ((it.team || '').trim() || '未标注团队') === TM_TEAM);
  }

  /* 频道筛选（键是「团队 / 频道」） */
  if(ch){
    items = items.filter(it=>{
      const c = (it.channel||'').trim() || '未标注频道';
      const t = (it.team||'').trim();
      return (t ? `${t} / ${c}` : c) === ch;
    });
  }

  /* 搜索：标题 / 正文 / PDF 文字 / 频道 / 人名 / 附件名 / 回复正文 / 日期
     ★ 多词按「同时命中」处理（空格、逗号、顿号、分号都算分隔） */
  const terms = kw ? kw.toLowerCase().split(/[\s,，、;；]+/).filter(Boolean) : [];
  if(terms.length){
    items = items.filter(it => {
      const atts = (it.attachments || []).map(a => a.name || '').join(' ');
      const reps = (it.replies || []).map(r =>
        (r.author || '') + ' ' + (r.text || '')).join(' ');
      const blob = ((it.title||'') + ' ' + (it.text||'') + ' ' +
                    (it.file_text||'') + ' ' + (it.file_note||'') + ' ' +
                    (it.channel||'') + ' ' + (it.author||'') + ' ' +
                    (it.date||'') + ' ' + atts + ' ' + reps).toLowerCase();
      return terms.every(t => blob.indexOf(t) >= 0);
    });
  }

  const chans = tmChannels().filter(([key]) =>
    !TM_TEAM || key.indexOf(TM_TEAM + ' / ') === 0);

  /* ── 缓存新鲜度提示 ── */
  let stale = '';
  const at = (TMP_STATUS_AT || '').trim();
  if(at){
    const m = at.match(/(\d{4})-(\d{2})-(\d{2})/);
    if(m){
      const then = new Date(+m[1], +m[2]-1, +m[3]);
      const days = Math.floor((Date.now() - then.getTime()) / 86400000);
      if(days >= 3){
        stale = `<div class="tpg-stale">
          ${icon('i-refresh','sm')}
          <span>缓存是 <b>${days} 天前</b> 的（${tmEsc(at.slice(0,10))}）。
          搜索只能搜到<b>已经抓下来</b>的消息：点「全部收一遍」更新最近的，
          想搜更早的历史就点「深度归档」补全。</span>
        </div>`;
      }
    }
  }

  /* ── 左栏：图标导航（私聊与日历不提供，置灰说明原因） ── */
  const rail = `<aside class="tmx-rail">
    <button class="tmx-ico on" title="团队与频道">${icon('i-teams','sm')}<span>团队</span></button>
    <button class="tmx-ico" data-view="files" title="文件：所有频道的附件">${icon('i-folder','sm')}<span>文件</span></button>
    <button class="tmx-ico dim" disabled title="私聊不在本程序范围内">${icon('i-list','sm')}<span>聊天</span></button>
    <button class="tmx-ico dim" disabled title="日历不在本程序范围内">${icon('i-hourglass','sm')}<span>日历</span></button>
    <div class="tmx-railsp"></div>
    <button class="tmx-ico" id="tpgOpen" title="在浏览器里打开 Teams">${icon('i-external','sm')}<span>打开</span></button>
  </aside>`;

  /* ── 团队列 ── */
  const teams = tmTeams();
  let sideTeams = '<aside class="tmx-col tmx-teams">';
  sideTeams += `<div class="tmx-colhead">团队<i>${teams.length}</i></div>`;
  sideTeams += `<button class="tmx-row tpg-team ${!TM_TEAM?'on':''}" data-tm="">
      <span class="tmx-av">全</span><span class="tmx-nm">全部团队</span>
      <i>${(TM_ITEMS||[]).length}</i></button>`;
  teams.forEach(([name, n])=>{
    sideTeams += `<button class="tmx-row tpg-team ${TM_TEAM===name?'on':''}"
        data-tm="${tmEsc(name)}" title="${tmEsc(name)}">
      <span class="tmx-av">${tmEsc((name||'?').slice(0,1))}</span>
      <span class="tmx-nm">${tmEsc(name)}</span><i>${n}</i></button>`;
  });
  sideTeams += '</aside>';

  /* ── 频道列（选了团队才出现，和 Teams 一致） ── */
  let sideChans = '';
  if(chans.length){
    sideChans = '<aside class="tmx-col tmx-chans">';
    sideChans += `<div class="tmx-colhead">频道<i>${chans.length}</i></div>`;
    sideChans += `<button class="tmx-row tpg-ch ${!ch?'on':''}" data-ch="">
        <span class="tmx-hash">#</span><span class="tmx-nm">全部频道</span>
        <i>${(TM_ITEMS||[]).length}</i></button>`;
    chans.forEach(([key, n])=>{
      sideChans += `<button class="tmx-row tpg-ch ${ch===key?'on':''}"
          data-ch="${tmEsc(key)}" title="${tmEsc(key)}">
        <span class="tmx-hash">#</span><span class="tmx-nm">${tmEsc(tmChanShort(key))}</span>
        <i>${n}</i></button>`;
    });
    sideChans += '</aside>';
  }

  /* ── 主内容 ── */
  const curTeam = TM_TEAM || '全部团队';
  const curChan = ch ? tmChanShort(ch) : '全部频道';
  let h = `<div class="tmx-head">
    <div class="tmx-crumb">
      <span class="tmx-tname">${tmEsc(curTeam)}</span>
      <span class="tmx-sep">/</span>
      <span class="tmx-cname">#${tmEsc(curChan)}</span>
      <span class="tmx-cnt">${items.length} 条</span>
    </div>
    <div class="tmx-acts">
      <button class="tpg-btn primary" id="tpgFetch" title="遍历所有团队的所有频道，一次收全">
        ${icon('i-download','sm')}<span>全部收一遍</span></button>
      <button class="tpg-btn" id="tpgDeep"
              title="补全历史：一直滚到每个频道最早的消息。第一次会很慢（可能一小时）">
        ${icon('i-hourglass','sm')}<span>深度归档</span></button>
      <button class="tpg-btn" id="tpgSync" title="只读当前打开的频道（快）">
        ${icon('i-refresh','sm')}<span>同步</span></button>
      <button class="tpg-btn" id="tpgDL">
        ${icon('i-folder','sm')}<span>取附件</span></button>
    </div>
  </div>
  <div class="tpg-status" id="tpgStatus">${TMP_STATUS || '还没同步过'}</div>${stale}`;

  h += `<div class="tmx-tabs">
    <button class="tmx-tab ${TM_VIEW!=='files'?'on':''}" data-view="posts">
      ${icon('i-list','sm')}<span>帖子</span></button>
    <button class="tmx-tab ${TM_VIEW==='files'?'on':''}" data-view="files">
      ${icon('i-folder','sm')}<span>文件</span></button>
  </div>`;

  h += `<div class="tpg-search">
    <input id="tpgKw" type="text" placeholder="搜消息内容、人名、频道名、附件名"
           value="${tmEsc(TM_PAGE_KEY)}">
  </div>`;

  /* ── 文件分区：把所有附件拉平成清单（Teams 的「文件」页签）── */
  if(TM_VIEW === 'files'){
    const files = [];
    items.forEach(it=>{
      (it.attachments||[]).forEach(a=>{
        files.push({u:a.url||'', n:a.name||'附件', t:it.team||'', c:it.channel||'',
                    d:it.date||'', by:it.author||''});
      });
    });
    if(!files.length){
      h += `<div class="tm-empty">这个范围里没有附件</div>`;
    }else{
      h += `<div class="tmx-files">
        <div class="tmx-frow tmx-fhead">
          <span>文件名</span><span>来自</span><span>日期</span><span></span></div>`;
      files.forEach(f=>{
        h += `<div class="tmx-frow">
          <span class="tmx-fname">${icon('i-file','sm')}${tmEsc(f.n)}</span>
          <span class="tmx-fsrc" title="${tmEsc((f.t?f.t+' / ':'')+f.c)}">${tmEsc(f.c || f.t || '')}</span>
          <span class="tmx-fdate">${tmEsc((f.d||'').slice(0,10))}</span>
          <button class="tmx-fdl tm-att" data-u="${tmEsc(f.u)}" data-n="${tmEsc(f.n)}"
                  title="下载到「桌面/ManageBac 资料/Teams 附件」">${icon('i-download','sm')}</button>
        </div>`;
      });
      h += `</div>`;
    }
  }
  /* ── 帖子分区（原有卡片，保持不动）── */
  else if(!items.length){
    h += (kw || ch)
      ? `<div class="tm-empty">没有匹配的内容</div>`
      : `<div class="tm-empty">
           <span class="oi">${icon('i-teams','xxl')}</span>
           <b>还没收过</b><br>
           先点「打开 Teams」登录一次，<br>
           再点「全部收一遍」——它会把所有团队、所有频道都读下来。<br>
           <span class="tpg-tip">之后消息都在这里看，不用再进 Teams 网页。</span>
         </div>`;
  }else{
    /*  按日期分组（团队与频道已经在左右两栏里选过了） */
    const byDate = {};
    items.forEach(it=>{
      const d = it.date || '未标注日期';
      (byDate[d] = byDate[d] || []).push(it);
    });
    Object.keys(byDate).sort().reverse().forEach(d=>{
      h += `<div class="tm-sec">${tmEsc(d)}
        <span class="tm-n">${byDate[d].length}</span></div>`;
      byDate[d].forEach((it, i)=>{
        const isEc = it.kind === 'ec';
        const isHw = it.kind === 'homework';
        const txt = (it.text || '').slice(0, 1200);
        const atts = it.attachments || [];
        const hasFile = !!(it.file_text || '').trim();
        const reps = it.replies || [];
        const ini = tmEsc(((it.author||'').trim().slice(0,1)) || '·');

        h += `<div class="tm-item" data-tp="${tmEsc(it.team||'')}|${tmEsc(it.channel||'')}|${tmEsc(d)}|${i}">
          <div class="tm-h">
            <span class="tmx-av sm">${ini}</span>
            <span class="tm-t">${tmHilite(it.title || '消息', kw)}</span>
            <span class="tmx-by">${tmHilite(it.author||'', kw)}</span>
            <span class="tm-d">${tmEsc((it.timeLabel||'').slice(0,18))}</span>
          </div>
          ${txt ? `<div class="tm-p">${tmHilite(txt, kw)}</div>` : ''}
          ${tmReactions(it.reactions)}
          ${reps.length ? `<div class="tm-reps">
            ${reps.slice(0, 3).map(r=>`
              <div class="tm-rep">
                <b>${tmHilite(r.author||'', kw)}</b>
                <span class="tm-rt">${tmEsc((r.time||'').slice(0,14))}</span>
                <div>${tmHilite((r.text||'').slice(0, 500), kw)}</div>
                ${tmReactions(r.reactions, true)}
              </div>`).join('')}
            ${reps.length > 3 ? `<div class="tm-rmore">展开全部 ${reps.length} 条回复</div>` : ''}
          </div>` : ''}
          <div class="tm-m">
            ${isEc ? '<span class="tm-tag ec">EC</span>' : ''}
            ${isHw ? '<span class="tm-tag hw">作业</span>' : ''}
            ${reps.length ? `<span class="tm-tag">${reps.length} 回复</span>` : ''}
            ${atts.length ? `<span class="tm-tag att">附件 ${atts.length}</span>` : ''}
            ${hasFile ? '<span class="tm-tag pdf">已读 PDF</span>' : ''}
          </div>
          ${atts.length ? `<div class="tm-atts">${atts.map(a=>
            `<button class="tm-att" data-u="${tmEsc(a.url||'')}" data-n="${tmEsc(a.name||'')}"
               title="点击下载到「桌面/ManageBac 资料/Teams 附件」">
               ${icon('i-download','sm')}<span>${tmEsc((a.name||'附件').slice(0,72))}</span>
             </button>`).join('')}</div>` : ''}
          ${hasFile ? `<div class="tm-file">
            ${it.file_note ? `<span class="tm-note">${tmEsc(it.file_note)}</span>` : ''}
            ${tmHilite(it.file_text.slice(0, 8000), kw)}
          </div>` : ''}
        </div>`;
      });
    });
  }

  box.innerHTML = `<div class="tmx">${rail}${sideTeams}${sideChans}
    <div class="tmx-main">${h}</div></div>`;

  /* ── 绑定（与原实现保持一致） ── */
  box.querySelectorAll('.tm-item').forEach(el=>{
    el.addEventListener('click', e=>{
      const att = e.target.closest && e.target.closest('.tm-att');
      if(att){ e.stopPropagation(); tmDownloadAtt(att); return; }
      if(e.target.tagName === 'MARK') return;
      el.classList.toggle('open');
    });
  });

  /*  文件分区里的下载按钮：不走 .tm-item 那条路径 */
  box.querySelectorAll('.tmx-fdl').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      tmDownloadAtt(el);
    });
  });

  box.querySelectorAll('.tpg-team').forEach(el=>{
    el.addEventListener('click', ()=>{
      TM_TEAM = el.dataset.tm || '';
      TM_CH = '';
      tmRenderPage();
    });
  });

  box.querySelectorAll('.tpg-ch').forEach(el=>{
    el.addEventListener('click', ()=>{
      TM_CH = el.dataset.ch || '';
      tmRenderPage();
    });
  });

  box.querySelectorAll('[data-view]').forEach(el=>{
    el.addEventListener('click', ()=>{
      TM_VIEW = el.dataset.view === 'files' ? 'files' : 'posts';
      tmRenderPage();
    });
  });

  const kwi = $('#tpgKw');
  if(kwi){
    kwi.addEventListener('input', ()=>{
      TM_PAGE_KEY = kwi.value;
      const pos = kwi.selectionStart;
      clearTimeout(tmRenderPage._t);
      tmRenderPage._t = setTimeout(()=>{
        tmRenderPage();
        const k2 = $('#tpgKw');
        if(k2){ k2.focus(); try{ k2.setSelectionRange(pos, pos); }catch(e){} }
      }, 260);
    });
  }

  const bind = (id, fn) => {
    const b = $('#' + id);
    if(!b || b.dataset.bound) return;
    b.dataset.bound = '1';
    b.addEventListener('click', fn);
  };
  bind('tpgOpen', ()=> tmPageAction('open'));
  bind('tpgFetch', ()=> tmFetchAll());
  bind('tpgDeep', ()=> tmDeepArchive());
  bind('tpgSync', ()=> tmPageAction('sync', 8));
  bind('tpgDL',   ()=> tmPageAction('dl'));
}

/* ════════════════════════════════════════════════════════════════
   「全部收一遍」—— 遍历所有团队的所有频道

   与「同步」的区别：
     同步       = 只读当前在 Teams 里打开的那个频道
     全部收一遍 = 自己进每个团队、收集频道 ID，再逐个频道取数
                  （用的是频道深度链接，所以不需要你去点）

   17 个频道实测约 280 秒。放在后端线程里跑，前端每 2 秒问一次进度。
   ════════════════════════════════════════════════════════════════ */
/*  ════════════════════════════════════════════════════════════════
   点附件即下载

   目标是「所有的『收』都在小组件里完成」——
   看到附件就能拿到手，不用再去 Teams 里翻。

   落到「桌面 / ManageBac 资料 / Teams 附件 / 原文件名」。
   后端 download_one 会先带会话 Cookie 直连，
   失败再退回浏览器内 fetch（SharePoint 常常只认后者）。
   ════════════════════════════════════════════════════════════════ */
async function tmDownloadAtt(btn){
  const url = btn.dataset.u || '';
  const name = btn.dataset.n || '';
  if(!url){ tmPageStatus('这条附件没有可下载的链接', false); return; }
  if(btn.dataset.busy) return;          /* 同一个按钮不重复点 */
  btn.dataset.busy = '1';
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="tm-att-run">下载中…</span>';
  try{
    /*  ★ 变量名不能叫 r：api 面检查器会把「含 pywebview+api 的赋值目标」
        当成 api 别名，文件里别处的 r.json() 就会被误判成后端方法。 */
    const dlr = window.pywebview?.api?.teams_download
      ? await window.pywebview.api.teams_download(url, name)
      : {ok:false, error:'当前模式不支持'};
    if(dlr && dlr.ok){
      btn.innerHTML = `<span class="tm-att-ok">已下载 ${tmEsc(dlr.name || '')}</span>`;
      tmPageStatus(`已下载：${dlr.path || ''}`, true);
    }else{
      btn.innerHTML = old;
      tmPageStatus(`下载失败：${(dlr && dlr.error) || '未知原因'}`, false);
    }
  }catch(e){
    btn.innerHTML = old;
    tmPageStatus('下载失败：' + e, false);
  }finally{
    delete btn.dataset.busy;
  }
}

/*  ════════════════════════════════════════════════════════════════
    深度归档 —— 补全历史消息

    【为什么需要它】
      「全部收一遍」是**增量**的：每个频道一滚到「整屏都是抓过的消息」
      就停。日常很省时间，但它意味着本地只攒「程序运行时出现过的新消息」。
      老消息永远不会进来 —— 于是搜索搜不到它们。

      实测：101 大群里有一条 9 月的「征集林里鸭子…」，
      搜「林里」搜不到 —— 不是搜索坏了，是它从没被抓过。

    【代价】
      每个频道滚到最早，第一次可能跑一小时。
      抓完仍然 merge，只增不减；之后日常用「全部收一遍」就行。
    ════════════════════════════════════════════════════════════════ */
async function tmDeepArchive(){
  if(tmDeepArchive._busy){
    //  ★ 已经在归档 → 这次点击就是「停止」★
    try{
      await apiTmCrawlCancel();
      tmPageStatus('正在停止…已抓到的消息会保留', true);
    }catch(e){}
    return;
  }
  if(!confirm(
      '深度归档会把每个频道的消息一直滚到最早，补全历史。\n\n' +
      '第一次可能要跑一小时左右，期间可以正常用电脑。\n' +
      '抓完只增不减，不会丢数据。\n\n确定现在开始吗？')){
    return;
  }
  tmDeepArchive._busy = true;

  const btn = $('#tpgDeep');
  const old = btn ? btn.innerHTML : '';
  if(btn){
    btn.classList.add('busy');
    btn.innerHTML = icon('i-hourglass','sm') + '<span>归档中…点此停止</span>';
    btn.title = '再点一次即可停止（已抓到的消息会保留）';
  }
  tmPageStatus('深度归档开始：将逐个频道滚到最早，请耐心等待…', true);

  try{ await apiTeamsOpen(); }catch(e){}

  let done = false;

  /*  同 tmFetchAll：轮询要在阻塞调用之前挂上，否则全程无进度 */
  clearInterval(TM_CRAWL_TIMER);
  const t0 = Date.now();
  TM_CRAWL_TIMER = setInterval(async ()=>{
    if(done) return;
    try{
      const s = await apiTmCrawlStatus();
      const el = Math.floor((Date.now() - t0) / 1000);
      const spent = el >= 60 ? `${Math.floor(el/60)} 分 ${el%60} 秒`
                             : `${el} 秒`;
      const msg = (s && s.message) ? tmEsc(s.message) : '正在归档…';
      tmPageStatus(`${msg}　<span class="tpg-elapsed">已用 ${spent}</span>`, true);
      if(s && !s.running){ clearInterval(TM_CRAWL_TIMER); done = true; }
    }catch(e){}
  }, 2000);

  const res = await apiTmCrawl(30, true);
  clearInterval(TM_CRAWL_TIMER);
  done = true;

  if(btn){ btn.classList.remove('busy'); btn.innerHTML = old; }
  tmDeepArchive._busy = false;

  if(res && res.ok){
    const nP = res.nPosts || 0;
    const head = res.cancelled ? '深度归档已停止' : '深度归档完成';
    tmPageStatus(
      `${head}：${res.nTeams || 0} 个团队 · ${res.nChannels || 0} 个频道 · ` +
      `<b>${nP}</b> 条消息（含历史）` +
      (res.cancelled ? '　<span class="tpg-elapsed">已抓到的都已保存</span>' : ''),
      true);
    await tmLoad(true);
  }else{
    tmPageStatus(`深度归档失败：${(res && res.reason) || '未知原因'}`, false);
  }
}

async function tmFetchAll(){
  if(tmFetchAll._busy) return;
  tmFetchAll._busy = true;

  const btn = $('#tpgFetch');
  const setBtn = (txt, spin)=>{
    if(!btn) return;
    btn.classList.toggle('busy', !!spin);
    btn.innerHTML = icon(spin ? 'i-refresh' : 'i-download', 'sm') +
                    `<span>${txt}</span>`;
  };

  setBtn('收集中…', true);
  ['tpgOpen','tpgSync','tpgDL'].forEach(id=>{
    const b = $('#'+id); if(b) b.disabled = true;
  });
  tmPageStatus('正在整理团队与频道…', true);

  /*  先确保 Teams 窗口是开着的（没开的话后端会自己拉起来） */
  try{ await apiTeamsOpen(); }catch(e){}

  let done = false;

  /*  ★★ 轮询必须在 await 之前挂上 ★★
      apiTmCrawl 是**阻塞**调用（后端 join 抓取线程），
      实测 17 个频道要 5~10 分钟。把 setInterval 写在它后面，
      等于整个抓取过程一个进度都看不到 ——
      状态栏一直停在「正在整理团队与频道…」，用户只会觉得「点了没反应」。
      顺带显示已耗时，让人确定它没死。 */
  clearInterval(TM_CRAWL_TIMER);
  const t0 = Date.now();
  TM_CRAWL_TIMER = setInterval(async ()=>{
    if(done) return;
    try{
      const s = await apiTmCrawlStatus();
      const el = Math.floor((Date.now() - t0) / 1000);
      const spent = el >= 60 ? `${Math.floor(el/60)} 分 ${el%60} 秒`
                             : `${el} 秒`;
      const msg = (s && s.message) ? tmEsc(s.message) : '正在整理团队与频道…';
      tmPageStatus(`${msg}　<span class="tpg-elapsed">已用 ${spent}</span>`, true);
      if(s && !s.running){
        clearInterval(TM_CRAWL_TIMER);
        done = true;
      }
    }catch(e){}
  }, 2000);

  const res = await apiTmCrawl(10);
  clearInterval(TM_CRAWL_TIMER);
  done = true;

  setBtn('全部收一遍', false);
  ['tpgOpen','tpgSync','tpgDL'].forEach(id=>{
    const b = $('#'+id); if(b) b.disabled = false;
  });
  tmFetchAll._busy = false;

  if(res && res.ok){
    const nP = res.nPosts || 0;
    const nR = res.nReplies || 0;
    const nT = res.nTeams || 0;
    const nC = res.nChannels || 0;
    tmPageStatus(
      `收好了 —— <b>${nT}</b> 个团队 · <b>${nC}</b> 个频道 · ` +
      `<b>${nP}</b> 条消息 · <b>${nR}</b> 条回复` +
      (res.partial ? '（到时间上限，下次接着收）' : ''), true);
    await tmLoad(true);
  }else{
    tmPageStatus('没收成：' + tmEsc((res && res.reason) || '未知原因'));
  }
}

/* 整页状态栏（与弹窗的状态分开存，互不覆盖） */
let TMP_STATUS = '';

function tmPageStatus(html, ok){
  TMP_STATUS = html;
  const el = $('#tpgStatus');
  if(el){
    el.innerHTML = html;
    el.classList.toggle('on', !!ok);
  }
}

/* 整页上的四个动作（复用弹窗那套 API） */
async function tmPageAction(what, rounds){
  if(tmPageAction._busy) return;
  const btns = ['tpgOpen','tpgFetch','tpgSync','tpgDL'].map(id=>$('#'+id));
  const disable = (v) => btns.forEach(b=>{ if(b) b.disabled = v; });

  if(what === 'open'){
    tmPageStatus('正在打开 Teams 窗口…');
    disable(true);
    try{
      const r = await apiTeamsOpen();
      tmPageStatus(r && r.ok
        ? '窗口已打开。在里面登录并点开一个会话，然后回来点「同步」。'
        : '打开失败：' + ((r && r.error) || '未知原因'));
    }catch(e){ tmPageStatus('打开失败：' + e); }
    finally{ disable(false); }
    return;
  }

  if(what === 'dl'){
    tmPageStatus('正在下载附件并读取 PDF…');
    disable(true);
    try{
      const r = await apiTeamsDownload();
      if(r && r.ok){
        tmPageStatus(`已下载 <b>${r.downloaded}</b> / ${r.total} 个附件 · `
          + `读到文字 <b>${r.text_extracted}</b> 份`, true);
        toast('附件处理完成', 'ok', '');
      }else{
        tmPageStatus('未完成：' + ((r && r.reason) || '未知原因'));
      }
      await tmLoad(true);
    }catch(e){ tmPageStatus('出错：' + e); }
    finally{ disable(false); }
    return;
  }

  /* 同步 */
  tmPageAction._busy = true;
  disable(true);
  tmPageStatus(rounds > 10
    ? '正在往更深处翻并读取……可能要一两分钟'
    : '正在往回翻并读取页面……大约 15~40 秒');

  try{
    const r = await apiTeamsSync(rounds);
    if(r && r.ok){
      const roundsTxt = r.scroll_rounds
        ? ` · 往回翻 <b>${r.scroll_rounds}</b> 轮`
          + (r.scroll_grew ? `（<b>${r.scroll_grew}</b> 轮有新内容）`
                           : '（没有更早的）')
        : '';
      tmPageStatus(
        `读了 <b>${r.scanned||0}</b> 条 · `
        + `EC <b>${r.ec||0}</b> · 其他 <b>${r.posts||0}</b> · `
        + `附件 <b>${r.attachments||0}</b>` + roundsTxt
        + (r.at_top ? ' · <b>已到最早</b>' : ''), true);
      toast('Teams 同步完成', 'ok', '');
    }else{
      const why = (r && r.reason) || '未知原因';
      tmPageStatus('未完成：' + why);
      /*  busy = 互斥闸提示「另一个 Teams 操作在跑」，不是出错 */
      if(!(r && r.busy)) toast('同步未完成', 'err', '');
    }
    await tmLoad(true);
  }catch(e){
    tmPageStatus('出错：' + e);
  }finally{
    tmPageAction._busy = false;
    disable(false);
  }
}

function openTeams(){
  const host = $('#teampanel');
  if(host) host.classList.add('open');  tmLoad();
}

function closeTeams(){
  $('#teampanel')?.classList.remove('open');
}

function bindTeams(){
  /*  幂等：只绑一次。
      页面里有两个 DOMContentLoaded 监听器都会调到这里，
      重复绑定会让部分逻辑互相干扰（重建 DOM 会丢监听器）。
      踩过的坑：表现为「点了没反应」且不报错。 */
  if(bindTeams._done) return;
  bindTeams._done = true;

  const btn = $('#btnTeams');

  btn?.addEventListener('click', e=>{
    e.stopPropagation();
    if($('#teampanel')?.classList.contains('open')) closeTeams();
    else openTeams();
  });

  $('#tmClose')?.addEventListener('click', closeTeams);
  $('#teampanel')?.addEventListener('click', e=>{
    if(e.target && e.target.id === 'teampanel') closeTeams();
  });

  /* 打开 Teams（浏览器窗口） */
  $('#tmOpen')?.addEventListener('click', async e=>{
    e.stopPropagation();
    const b = $('#tmOpen');
    b?.classList.add('busy');
    tmSetStatus('正在打开 Teams 窗口……');
    try{
      const r = await apiTeamsOpen();
      tmSetStatus(r && r.ok
        ? '窗口已打开，在里面登录 Teams，然后回来点「同步」'
        : '打开失败：' + ((r && r.error) || '未知原因'));
    }catch(err){
      tmSetStatus('打开失败：' + err);
    }finally{
      b?.classList.remove('busy');
    }
  });

  /* 同步（共用函数：普通同步 / 多翻一点） */
  async function doSync(rounds, btn){
    if(btn?.classList.contains('busy')) return;
    btn?.classList.add('busy');
    $('#tmSync')?.classList.add('busy');
    $('#tmMore')?.classList.add('busy');
    tmSetStatus(rounds > 10
      ? '正在往更深处翻，并读取页面……可能要一两分钟'
      : '正在往回翻并读取页面……大约 15~40 秒');

    try{
      const r = await apiTeamsSync(rounds);
      if(r && r.ok){
        /*  如实报告：翻了几轮、有几轮真的加载出了新内容、到没到最早 */
        const roundsTxt = r.scroll_rounds
          ? ` · 往回翻 <b>${r.scroll_rounds}</b> 轮` +
            (r.scroll_grew ? `（<b>${r.scroll_grew}</b> 轮有新内容）` : '（没有更早的）')
          : '';
        tmSetStatus(
          `同步完成 · 读了 <b>${r.scanned||0}</b> 条 · ` +
          `EC <b>${r.ec||0}</b> · 其他 <b>${r.posts||0}</b> · ` +
          `附件 <b>${r.attachments||0}</b>` +
          roundsTxt +
          (r.at_top ? ' · <b>已到最早</b>' : ''), true);
        toast('Teams 同步完成', 'ok', '');
      }else{
        const why = (r && r.reason) || '未知原因';
        tmSetStatus('未完成：' + why);
        /*  busy 是互斥闸在说话（「另一个 Teams 操作正在跑」）——
            那是「稍后再试」，不是出错，不弹红 toast 吓人 */
        if(!(r && r.busy)) toast('同步未完成：' + why, 'err', '');
      }
      await tmLoad(true);   /* 只刷新列表，保住刚同步出来的状态文字 */
      tmRender();
    }catch(err){
      tmSetStatus('出错：' + err);
    }finally{
      btn?.classList.remove('busy');
      $('#tmSync')?.classList.remove('busy');
      $('#tmMore')?.classList.remove('busy');
    }
  }

  $('#tmSync')?.addEventListener('click', e=>{
    e.stopPropagation();
    doSync(8, $('#tmSync'));
  });

  /* 多翻一点：往更深处翻，拿更早的历史 */
  $('#tmMore')?.addEventListener('click', e=>{
    e.stopPropagation();
    doSync(25, $('#tmMore'));
  });

  /* 下载 EC 名单 PDF + 提取文字 + 扫名字 */
  $('#tmDL')?.addEventListener('click', async e=>{
    e.stopPropagation();
    const b = $('#tmDL');
    if(b?.classList.contains('busy')) return;
    b?.classList.add('busy');
    tmSetStatus('正在取 EC 名单 PDF……');

    /*  这一趟要开浏览器 + 逐份下载，可能几十秒。
        所以起一个轮询，把后端进度显示出来 ——
        不然用户会以为卡死了。 */
    let tick = null;
    const apiProg = window.pywebview?.api?.ec_download_status;
    if(apiProg){
      tick = setInterval(async ()=>{
        try{
          const st = await apiProg();
          if(st && st.running && st.message) tmSetStatus(st.message);
        }catch(_){}
      }, 1500);
    }

    try{
      const r = await apiTeamsDownload();
      if(r && r.ok){
        const parts = [];
        if(r.extracted) parts.push(`读到 <b>${r.extracted}</b> 份名单`);
        if(r.downloaded) parts.push(`新下载 <b>${r.downloaded}</b> 份`);
        tmSetStatus(parts.length ? parts.join(' · ') : '没有新的名单',
                    true);

        /*  命中就报出来 —— 这是用户填名字的**唯一目的** */
        const w = r.watch || {};
        if(w.alert && w.found && w.found.length){
          toast('EC 名单上有你：' + w.found.join('、'), 'warn', '');
        }else if(r.note){
          toast('EC 名单', 'info', r.note);
        }else if(r.extracted){
          toast('名单里没有你', 'ok', '');
        }

        /*  有需要手动过 MFA 的，单独说清（别混在 toast 里一笔带过）*/
        if(r.need_login && r.need_login.length){
          tmSetStatus(
            `另有 <b>${r.need_login.length}</b> 份在站点文档库里，`
            + `需要先完成一次 Microsoft 二次验证（MFA）。`
            + `<br><span class="dim">${r.need_login.map(esc).join('、')}`
            + `</span>`, true);
        }
      }else{
        tmSetStatus('未完成：' + ((r && (r.note || r.reason)) || '未知原因'));
      }
      await tmLoad(true);   /* 只刷新列表，保住刚处理完的状态文字 */
      tmRender();
      ecRefresh();          /* 顺手刷一下顶栏红点 */
    }catch(err){
      tmSetStatus('出错：' + err);
    }finally{
      if(tick) clearInterval(tick);
      b?.classList.remove('busy');
    }
  });

  /* 搜索（防抖 200ms，避免每敲一个字就重绘） */
  let tmTimer = null;
  $('#tmSearch')?.addEventListener('input', e=>{
    const v = e.target.value || '';
    clearTimeout(tmTimer);
    tmTimer = setTimeout(()=>{ TM_KEY = v; tmRender(); }, 200);
  });
}

/* ══════════ 自编课表面板 ══════════
   用途：添加网站课表里没有的课（上操、社团、班会）。

    交互设计
     · 星期几用一排可点的胶囊（多选）—— 因为「上操」这种通常是
       每周一三五，多选一次配好就不用天天加
     · 第几节用下拉（节次表来自后端，抓不到就给 P1..P9 兜底）
     · 存 localStorage，不和抓取来的数据混在一起
     · 列表里能直接删 */

let clDays = [];          /* 当前选中的星期几 [1,3,5] */

function clPeriodsOptions(){
  /* 优先用后端抓到的节次表；没有就兜底 P1..P9 */
  const sch = (DATA && DATA.schedule) || {};
  const ps = (sch.periods || []).map(x => x.period);
  const list = ps.length ? ps
             : Array.from({length: 9}, (_, i) => 'P' + (i + 1));
  return list;
}

/* ══════════ 时刻 ↔ 节次 换算 ══════════
   自编课要支持两种填法，且两边联动：
     · 按节次：选 P3 → 自动带出 09:40–10:20
     · 按时刻：填 09:45 → 自动认出落在 P3
   时间表来自后端抓的 sch.periods；抓不到时退化成只有节次名。 */

/* "09:40" → 580（分钟数）。非法值返回 null。 */
function hm2min(s){
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s == null ? '' : s).trim());
  if(!m) return null;
  const h = +m[1], mi = +m[2];
  if(h > 23 || mi > 59) return null;
  return h * 60 + mi;
}

/* 580 → "09:40" */
function min2hm(v){
  const n = ((Math.round(v) % 1440) + 1440) % 1440;
  return String(Math.floor(n / 60)).padStart(2, '0') + ':'
       + String(n % 60).padStart(2, '0');
}

/* 节次表里某一节的起止时间；查不到返回 null */
function periodTimes(name){
  const sch = (DATA && DATA.schedule) || {};
  const key = String(name || '').trim().toUpperCase();
  if(!key) return null;
  return (sch.periods || []).find(
    x => String(x.period || '').toUpperCase() === key) || null;
}

/* 某一时刻最贴近哪一节（用于「按时刻」反推节次）
   命中区间内的优先；都不在区间里就取起点最近的。 */
function periodForTime(hm, endHm){
  const sch = (DATA && DATA.schedule) || {};
  const ps = sch.periods || [];
  if(hm2min(hm) == null || !ps.length) return '';
  /*  有结束时间 → 按重叠判定（最准）。
      只给了起点 → 退回「是不是落在某一节里」。 */
  if(endHm){
    const hit = bestPeriodByOverlap(hm, endHm, ps);
    if(hit) return hit.period || '';
  }
  const t = hm2min(hm);
  let best = '', bestD = Infinity;
  for(const p of ps){
    const a = hm2min(p.start), b = hm2min(p.end);
    if(a == null) continue;
    if(b != null && t >= a && t < b) return p.period || '';
    const d = Math.abs(t - a);
    if(d < bestD){ bestD = d; best = p.period || ''; }
  }
  return best;
}

/*  时刻 → 位置值。用来把「只有时间」的自编课插到恰当位置。
     整数 = 落在该节次里；x.5 = 插在第 x 节之后（两节之间）。 */
function timeToSlotPos(t, periods){
  for(let i = 0; i < periods.length; i++){
    const a = hm2min(periods[i].start);
    const b = hm2min(periods[i].end);
    if(a == null) continue;
    if(t < a) return i - 0.5;
    if(b == null || t <= b) return i;
  }
  return periods.length - 0.5;
}

/*  更准的落位：看「这节课的整段」和哪一节重叠最多。
     为什么不能只看起点：09:45 开始的社团活动，起点比 P3 早 5 分钟，
     按起点算会插到 P2 和 P3 中间 —— 但它 10:35 结束，
     正好是 P3 的下课点，明显就是 P3 的课。
     所以按重叠比例判定，重叠超过一半就认定属于那一节。 */
function bestPeriodByOverlap(start, end, periods){
  const a = hm2min(start);
  if(a == null) return null;
  let b = hm2min(end);
  if(b == null || b <= a) b = a + 45;      /* 没填结束就按一节 45 分钟估 */

  let best = null, bestOv = 0;
  for(const p of periods){
    const pa = hm2min(p.start);
    const pb = hm2min(p.end);
    if(pa == null || pb == null) continue;
    const ov = Math.min(b, pb) - Math.max(a, pa);     /* 重叠分钟数 */
    if(ov > bestOv){ bestOv = ov; best = p; }
  }
  const span = b - a;
  /*  重叠要过半才算「就是这一节」；否则说明它横跨两节或落在课间，
      交给 timeToSlotPos 按起点插到中间。 */
  if(best && span > 0 && bestOv / span >= 0.5) return best;
  return null;
}

function openCustomPanel(){
  const host = $('#clpanel');
  if(!host) return;

  /* 星期胶囊 */
  const dw = $('#clDays');
  if(dw && !dw.dataset.built){
    dw.dataset.built = '1';
    dw.innerHTML = ['一','二','三','四','五','六','日'].map((c, i) => {
      /* 注意：CL_WD 是 [周日..周六]，这里显示是 [周一..周日]，
         所以索引要换算 —— 显示第 i 个对应 CL_WD 的 (i+1)%7 */
      const wd = (i + 1) % 7;
      return `<span data-wd="${wd}">${c}</span>`;
    }).join('');
    dw.querySelectorAll('span').forEach(el => {
      el.addEventListener('click', () => {
        const wd = +el.dataset.wd;
        const k = clDays.indexOf(wd);
        if(k >= 0) clDays.splice(k, 1); else clDays.push(wd);
        el.classList.toggle('on', k < 0);
      });
    });
  }

  /* 节次下拉 */
  const sel = $('#clPeriod');
  if(sel){
    sel.innerHTML = clPeriodsOptions()
      .map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
  }

  clBindModes();
  clSyncFromPeriod();        /* 初始化时把提示刷出来 */

  clRenderList();
  host.classList.add('open');
  setTimeout(() => { try{ $('#clName')?.focus(); }catch(e){} }, 300);
}

/*  两种填法共用同一份数据：clMode 决定用哪一档，
    切换时互相带出对方的值（选了 P3 就填上 09:40，反之亦然）。 */
let clMode = 'period';

function clBindModes(){
  const box = $('#clMode');
  if(!box || box.dataset.built) return;
  box.dataset.built = '1';

  box.querySelectorAll('.clmode').forEach(b=>{
    b.addEventListener('click', ()=>{
      const m = b.dataset.mode;
      if(m === clMode) return;

      /*  切换时以「刚填的那一档」为准，带出另一档的值。
          关键：目标那一档的旧值要**覆盖**而不是保留 ——
          否则切来切去会留下上一次填的残值（曾出现过：
          切到按时刻后，显示的还是几轮前填的 13:00）。 */
      if(m === 'time'){
        const pt = periodTimes($('#clPeriod')?.value);
        const s = $('#clStart'), e = $('#clEnd');
        if(pt && s && e){
          s.value = pt.start || '';
          e.value = pt.end   || '';
        }
      }else{
        const st = $('#clStart')?.value || '';
        const en = $('#clEnd')?.value   || '';
        const p = st ? periodForTime(st, en) : '';
        if(p) $('#clPeriod').value = p;
      }

      clMode = m;
      box.querySelectorAll('.clmode').forEach(x=>
        x.classList.toggle('on', x.dataset.mode === m));
      const rp = $('#clRowPeriod'), rt = $('#clRowTime');
      if(rp) rp.hidden = (m !== 'period');
      if(rt) rt.hidden = (m !== 'time');
      if(m === 'period') clSyncFromPeriod(); else clSyncFromTime();
    });
  });

  /* 节次变了 → 带出时间 */
  $('#clPeriod')?.addEventListener('change', clSyncFromPeriod);

  /* 时刻变了 → 反推节次；两端互相补全 */
  $('#clStart')?.addEventListener('input', ()=>{
    const a = hm2min($('#clStart')?.value);
    const b = hm2min($('#clEnd')?.value);
    /* 结束时间比开始早（或没填）→ 自动补一节 45 分钟 */
    if(a != null && (b == null || b <= a)){
      const e = $('#clEnd');
      if(e) e.value = min2hm(a + 45);
    }
    clSyncFromTime();
  });
  $('#clEnd')?.addEventListener('input', ()=>{
    const a = hm2min($('#clStart')?.value);
    const b = hm2min($('#clEnd')?.value);
    /*  结束早于（或等于）开始 → 直接纠正成「开始 + 一节课」。
        课表不会跨天，所以这里不用问，改了就是。 */
    if(a != null && b != null && b <= a){
      const e = $('#clEnd');
      if(e) e.value = min2hm(a + 45);
      clSyncFromTime();
      toast('结束时间改到 ' + min2hm(a + 45), 'ok', '');
      return;
    }
    clSyncFromTime();
  });
}

/* 选了节次 → 提示里显示对应时刻 */
function clSyncFromPeriod(){
  const hint = $('#clPHint');
  if(!hint) return;
  const name = $('#clPeriod')?.value || '';
  const pt = periodTimes(name);
  if(pt && pt.start){
    hint.innerHTML = `${esc(name)} 是 <b>${esc(pt.start)}` +
      (pt.end ? `–${esc(pt.end)}` : '') + `</b>，会自动落在这里。`;
    hint.classList.remove('warn');
  }else{
    hint.innerHTML = '没抓到这节课的时间表，会按节次名排在对应位置。';
    hint.classList.add('warn');
  }
}

/* 填了时刻 → 反推节次并提示 */
function clSyncFromTime(){
  const hint = $('#clTHint');
  if(!hint) return;
  const st = $('#clStart')?.value || '';
  const en = $('#clEnd')?.value || '';
  const a = hm2min(st);
  if(a == null){
    hint.innerHTML = '填好起止时刻，会自动认出对应的节次。';
    hint.classList.remove('warn');
    return;
  }
  const p = periodForTime(st, en);
  const pt = periodTimes(p);
  const parts = [];
  if(p){
    parts.push(`会自动落到 <b>${esc(p)}</b>`);
  }else{
    parts.push('没比对出节次，会按时间插在对应位置');
  }
  if(pt && pt.start){
    const pa = hm2min(pt.start);
    const diff = pa == null ? null : a - pa;
    if(diff != null && Math.abs(diff) > 3){
      const sign = diff > 0 ? '晚' : '早';
      parts.push(`比这一节${sign} ${Math.abs(diff)} 分钟`);
    }
  }
  if(en){
    const b = hm2min(en);
    if(b != null && b <= a) parts.push('结束时间比开始早');
  }
  hint.innerHTML = parts.join(' · ');
  hint.classList.toggle('warn', !p);
}

function closeCustomPanel(){
  $('#clpanel')?.classList.remove('open');
}

function clRenderList(){
  const box = $('#clList');
  if(!box) return;
  const list = customLessons();
  if(!list.length){
    box.innerHTML = '';
    return;
  }
  let h = `<div class="clt">已添加 ${list.length} 条</div>`;
  list.forEach((c, idx) => {
    const when = c.once
      ? (c.day || '')
      : (c.weekdays || []).map(w => CL_WD[w]).join('、');
    /* 时间那一段：有节次显示节次，没节次就显示时刻 */
    const pt = periodTimes(c.period);
    const st = c.start || (pt && pt.start) || '';
    const en = c.end   || (pt && pt.end)   || '';
    let timeTxt = c.period || '';
    if(st){
      timeTxt = (c.period ? c.period + ' ' : '') + st + (en ? '–' + en : '');
    }
    h += `<div class="clitem">
      <span class="cln">${esc(timeTxt)}${timeTxt ? ' · ' : ''}${esc(c.subject || '')}${
        c.room ? ` · ${esc(c.room)}` : ''}</span>
      <span class="clw">${esc(when)}</span>
      <button class="cld" data-cldel="${idx}" title="删除"></button>
    </div>`;
  });
  box.innerHTML = h;

  box.querySelectorAll('[data-cldel]').forEach(b => {
    b.addEventListener('click', e => {
      e.stopPropagation();
      const list2 = customLessons();
      list2.splice(+b.dataset.cldel, 1);
      saveCustomLessons(list2);
      clRenderList();
      lastSig = '';            /* 强制重绘课表 */
      render();
    });
  });
}

function clSave(){
  const name  = ($('#clName')?.value || '').trim();
  const room  = ($('#clRoom')?.value || '').trim();
  const byTime = (clMode === 'time');
  let per   = $('#clPeriod')?.value || '';
  let st    = '';
  let en    = '';

  if(byTime){
    st = $('#clStart')?.value || '';
    en = $('#clEnd')?.value   || '';
    /* 按时刻填的：反推一个节次名存着（列表里好认，也便于排序）
       带结束时间 → 用重叠判定，比只看起点准。 */
    const guess = periodForTime(st, en);
    if(guess) per = guess;
  }else{
    const pt = periodTimes(per);
    st = (pt && pt.start) || '';
    en = (pt && pt.end)   || '';
  }

  if(!name){
    toast('先填个名称', 'err', '');
    try{ $('#clName')?.focus(); }catch(e){}
    return;
  }
  if(!clDays.length){
    toast('选一下星期几', 'err', '');
    return;
  }
  /*  按节次填时必须有节次；按时刻填时必须有开始时间。
      两者都没有 → 存下来也没法落位，不如当场说清楚。 */
  if(byTime && hm2min(st) == null){
    toast('填一下开始时刻', 'err', '');
    try{ $('#clStart')?.focus(); }catch(e){}
    return;
  }
  /*  结束时间：没填或填反了，都按「开始 + 45 分钟」补正。
      不在这里拦人 —— 用户只想快速加一条时，多一次报错很烦。 */
  if(byTime){
    const a = hm2min(st), b = hm2min(en);
    if(b == null || b <= a) en = min2hm(a + 45);
  }
  if(!byTime && !per){
    toast('选一下第几节', 'err', '');
    return;
  }

  const list = customLessons();
  list.push({
    id: 'c-' + Date.now(),
    weekdays: clDays.slice().sort(),
    period: per,
    start: st,
    end: en,
    byTime: byTime,
    subject: name,
    room: room,
    flag: '',
  });
  saveCustomLessons(list);

  /* 清掉输入，方便连着加下一条（保留星期与模式，通常要连加几条） */
  try{
    $('#clName').value = '';
    $('#clRoom').value = '';
    $('#clStart').value = '';
    $('#clEnd').value = '';
  }catch(e){}

  clRenderList();
  clSyncFromPeriod();
  clSyncFromTime();
  toast('已添加：' + name, 'ok', '');

  lastSig = '';
  render();
}

function bindCustomPanel(){
  /*  幂等：只绑一次。
      页面里有两个 DOMContentLoaded 监听器都会调到这里，
      重复绑定会让部分逻辑互相干扰（重建 DOM 会丢监听器）。
      踩过的坑：表现为「点了没反应」且不报错。 */
  if(bindCustomPanel._done) return;
  bindCustomPanel._done = true;

  /* 按钮是动态渲染的（每次重绘课表都会重建），
     所以用**事件委托**挂在 document 上，而不是直接绑按钮。 */
  document.addEventListener('click', e => {
    const t = e.target;
    if(t && t.closest && t.closest('#clOpen')){
      e.stopPropagation();
      openCustomPanel();
    }
  });

  $('#clClose')?.addEventListener('click', closeCustomPanel);
  $('#clSave')?.addEventListener('click', clSave);

  /* 点遮罩关闭（点面板内部不关） */
  $('#clpanel')?.addEventListener('click', e => {
    if(e.target && e.target.id === 'clpanel') closeCustomPanel();
  });

  /* 回车直接保存（time 输入框回车不太顺手，也一起绑上） */
  ['#clName', '#clRoom', '#clStart', '#clEnd'].forEach(sel => {
    const el = $(sel);
    el?.addEventListener('keydown', e => {
      if(e.key === 'Enter'){ e.preventDefault(); clSave(); }
    });
  });

  /* ESC 关闭 */
  document.addEventListener('keydown', e => {
    if(e.key !== 'Escape') return;
    if($('#clpanel')?.classList.contains('open')) closeCustomPanel();
  });
}

/* ══════════ 折叠区块外壳 ══════════
    这个函数在拆文件时丢过一次（原 index.html 里就被截断了），
     从备份恢复。它是所有折叠区块（待办/测验/作业/课表分组）的容器。 */
/* ══════════ 阅读状态（0.5.2）══════════

   render() 在数据变化时会重建整块 DOM（#page-grade / #page-sch …）。
   数据每 5 分钟自动刷新一次，用户可能正好在滚动或折叠某个区块 ——
   重建后回到顶部、折叠全开，很难受。

   这里在重建**前**把三件事记下来，重建后放回去：
     ① 滚动位置：用**锚点元素**（第一个底边 > 0 的 [data-anchor]）
        及其 top —— 内容高度变了也能对得准，比只记 scrollTop 强
     ② 展开状态：[data-tg] 折叠区的开合（本项目存在 collapsed 里，
        本来就不丢，这里只是保证重建后类名对得上）
     ③ 局部滚动：[data-keep-scroll] 容器各自的 scrollTop

   ★ 不做选区/光标恢复 —— 本界面没有「边看边编辑长文本」的场景，
     而表单输入框都是独立浮层（不受重建影响）。
 */
function saveReading(host){
  if(!host) return null;
  try{
    const scroller = document.getElementById('viewport') || document.documentElement;
    const st = { top: scroller.scrollTop, anchors: [], inner: [] };
    /*  锚点：第一个还在视口下方的带标记元素，记下它离视口顶多远。
        重建后按同样的距离把它放回原处。 */
    const marks = host.querySelectorAll('[data-anchor]');
    for(const el of marks){
      const r = el.getBoundingClientRect();
      if(r.bottom > 0){
        st.anchors.push({ key: el.getAttribute('data-anchor'), off: r.top });
        break;
      }
    }
    host.querySelectorAll('[data-keep-scroll]').forEach((el, i)=>{
      st.inner.push({ i, top: el.scrollTop });
    });
    return st;
  }catch(e){ return null; }
}

function restoreReading(host, st){
  if(!host || !st) return;
  try{
    /* ① 局部滚动 */
    if(st.inner && st.inner.length){
      const boxes = host.querySelectorAll('[data-keep-scroll]');
      st.inner.forEach(x=>{
        const el = boxes[x.i];
        if(el) el.scrollTop = x.top;
      });
    }
    /* ② 滚动位置：优先用锚点对位 */
    const scroller = document.getElementById('viewport') || document.documentElement;
    if(st.anchors && st.anchors.length){
      const a = st.anchors[0];
      const el = host.querySelector(`[data-anchor="${CSS.escape(a.key)}"]`);
      if(el){
        const off = el.getBoundingClientRect().top;
        scroller.scrollTop += (off - a.off);
        return;
      }
    }
    /* 拿不到锚点就退回直接赋原值 */
    scroller.scrollTop = st.top;
  }catch(e){}
}

function section(id, title, countHtml, inner, extra=''){
  const isC = collapsed[id];
  return `<div class="sec ${isC?'collapsed':''}" data-anchor="sec-${id}">
    <div class="sechead" data-tg="${id}">
      <span class="arw">▼</span>
      <span class="t">${title}</span>
      ${countHtml}
      <span class="flex"></span>${extra}
    </div>
    <div class="secbody"><div class="secinner">${inner}</div></div>
  </div>`;
}

/* ══════════════════════════════════════════════════════════════════
   课表补全：空档填充 + 自编课表

    空档填充
     学校课表里没课的节次（如 P3），原来会直接跳过 ——
     于是 P2 数学、P4 化学看起来是连着的，看不出中间空了一节。
     现在把空档也占一行，标成「自习」。

    自编课表
     网站没有的课（上操、社团、班会），用户自己加。
     存在 localStorage，不污染抓取来的数据。
     · 单次：只加某一天
     · 每周：weekdays 里列出的星期几都显示
     ══════════════════════════════════════════════════════════════════ */

const CL_KEY = 'mb.custom.lessons';
const CL_WD  = ['周日','周一','周二','周三','周四','周五','周六'];

/* 读自编课 */
function customLessons(){
  try{
    const v = JSON.parse(localStorage.getItem(CL_KEY) || '[]');
    /*  只认数组。localStorage 可能被别的版本或手工改坏
        （存成对象、字符串），这时当空处理，不要抛异常 ——
        否则整个课表页都渲染不出来。 */
    if(!Array.isArray(v)) return [];
    return v.filter(x => x && typeof x === 'object');
  }catch(e){ return []; }
}
function saveCustomLessons(list){
  try{ localStorage.setItem(CL_KEY, JSON.stringify(list || [])); }catch(e){}
}

/* 自编课里，某一天该出现哪些 */
function customForDay(dayISO){
  const wd = new Date(dayISO + 'T00:00:00').getDay();   /* 0=周日 */
  return customLessons().filter(c=>{
    if(!c) return false;
    if(c.once) return c.day === dayISO;
    return (c.weekdays || []).indexOf(wd) >= 0;
  }).map(c=>{
    /*  起止时间的来源：
         ① 用户手填的 start/end（按时刻那一档）
         ② 没填就用节次表里这一节的时间
       两者都没有就留空 —— 后面按「不在节次表里」追加到末尾。 */
    const pt = periodTimes(c.period);
    return {
      /* 转成和抓取数据一样的结构，后面渲染就不用分叉了 */
      day: dayISO,
      period: c.period || '',
      subject: (c.flag ? '[' + c.flag + '] ' : '') + (c.subject || ''),
      room: c.room || '',
      teacher: '',
      start: c.start || (pt && pt.start) || '',
      end:   c.end   || (pt && pt.end)   || '',
      kind: '自定义',
      custom: true,
      byTime: !!c.byTime,   /* 按时刻填的 → 允许按时间落位 */
    };
  });
}

/* 给一天补空档 + 合并自编课
   periods: [{period:'P1', start:'08:00', end:'08:40'}, ...]（后端给的节次表）
   返回：按节次排好的完整列表 */
function fillDay(dayLessons, periods){
  /* 没有节次表 → 退化成旧行为（不补空档），但仍然合并自编课 */
  const my = periods || [];
  if(!my.length){
    return dayLessons.slice().sort((a,b)=>
      String(a.start||'').localeCompare(String(b.start||'')));
  }

  /* 按节次名索引已有的课（"P3" → lesson） */
  const byP = {};
  const noP = [];                     /* 不在节次表里的课 */
  for(const l of dayLessons){
    const key = (l.period || '').trim().toUpperCase();
    /*  ★ 自编课一律走「按时间落位」那条路（见下），
        不直接塞进 byP。原因：按时刻填的自编课也会带一个
        反推出来的节次名，若按名字归位，它就会和该节的真课
        并在一起、顺序不受控制。按时间算才能保证插在正确位置。 */
    if(key && !l.custom && my.some(x => x.period.toUpperCase() === key)){
      /* 同一节次可能有多门（冲突），用数组存 */
      (byP[key] = byP[key] || []).push(l);
    }else{
      noP.push(l);
    }
  }

  /*  没写节次的课：按时间算它该落在第几节。
      自编课按时刻填的时候走这条路 —— 用户填 09:45，
      它 10:35 结束，就该出现在 P3 那一行，而不是被丢到最后。

      判定顺序：
        ① 整段与某一节重叠过半 → 直接归到那一节
        ② 否则按起点算位置（x.5 = 插在两节之间）
        ③ 都算不出 → 追加到末尾 */
  const after = {};                   /* i → [插在第 i 节之后的课] */
  const tail = [];                    /* 实在算不出的，留在末尾 */
  const slotOf = {};
  my.forEach((pd, i)=>{ slotOf[pd.period.toUpperCase()] = i; });

  for(const l of noP){
    /*  时间来源：手填的 start/end，或（按节次填的）该节次的时间。
        customForDay 已经补过一遍，这里再兜一次底，
        保证「按时间落位」总有时间可算。 */
    let st = l.start, en = l.end;
    if(hm2min(st) == null){
      const pt = periodTimes(l.period);
      if(pt){ st = pt.start; en = en || pt.end; }
    }
    const t = hm2min(st);
    if(t == null){ tail.push(l); continue; }

    /* ① 整段与某一节重叠过半 → 就是那一节，和该节排在一起 */
    const hit = bestPeriodByOverlap(st, en, my);
    if(hit){
      const key = String(hit.period || '').toUpperCase();
      (byP[key] = byP[key] || []).push(l);
      continue;
    }

    /* ② 按起点插在两节之间 */
    const pos = timeToSlotPos(t, my);
    const idx = Math.floor(pos);
    const whole = Math.abs(pos - Math.round(pos)) < 0.001;
    if(whole && idx >= 0 && idx < my.length){
      const key = my[idx].period.toUpperCase();
      (byP[key] = byP[key] || []).push(l);
    }else if(idx >= 0 && idx < my.length){
      (after[idx] = after[idx] || []).push(l);
    }else{
      tail.push(l);
    }
  }

  /*  只填「两节真课之间」的空档。
     如果把节次表里 P1..P9 全铺满，当天最后一节课之后会出现
     一整片「自习」—— 很吵，也没有意义（还没上到那里）。
     所以先找出**第一门真课**和**最后一门真课**的位置，只在这段区间里补。 */
  const reallyUsed = my.map(pd => byP[pd.period.toUpperCase()])
                       .map((v, i) => (v && v.length) ? i : -1)
                       .filter(i => i >= 0);
  const lo = reallyUsed.length ? Math.min(...reallyUsed) : -1;
  const hi = reallyUsed.length ? Math.max(...reallyUsed) : -1;

  const out = [];
  my.forEach((pd, idx) => {
    const key = pd.period.toUpperCase();
    const hit = byP[key];
    if(hit && hit.length){
      out.push(...hit);
    }else if(idx > lo && idx < hi){
      /* 真课之间的空档 → 占位一行「自习」 */
      out.push({
        day: dayLessons[0] ? dayLessons[0].day : '',
        period: pd.period,
        subject: '自习',
        room: '', teacher: '',
        start: pd.start, end: pd.end,
        kind: '自习',
        selfStudy: true,
      });
    }
    /* 插在这一节之后的（按时刻算出来落在两节之间的） */
    if(after[idx]) out.push(...after[idx]);
  });
  /* 不在节次表里的（晚自习等）追加在后面，按时间排 */
  tail.sort((a,b)=>String(a.start||'').localeCompare(String(b.start||'')));
  out.push(...tail);
  return out;
}

/* 给整份课表做补全（按天） */
function completeSchedule(lessons, periods){
  const byDay = {};
  for(const l of lessons) (byDay[l.day] = byDay[l.day] || []).push(l);

  const allDays = new Set(Object.keys(byDay));
  const dayKeys = [...allDays].sort();
  const lastReal = dayKeys.length ? dayKeys[dayKeys.length - 1] : '';

  const isoOf = (dt) => `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
  const todayIso = isoOf(new Date());

  /*  ══════════ 范围门 ══════════
      加这个门的本意：不要让「每周一上操」铺到抓取范围之外，
      否则后续课程区会刷出一堆重复的周一，很吵。

      ★ 但它不该把自编课全挡掉 —— 曾出过的问题：
        抓取数据只到昨天时（数据还没刷新 / 预览环境），
        「今天」的课全被 continue 跳过，用户加的自编课根本不出现。
        所以门只在「抓取数据确实覆盖到今天或以后」时才生效。 */
  const gateOn = lastReal && lastReal >= todayIso;

  /* ① 滚动窗口：-1 ~ +15 天（装得下每周重复的自编课） */
  for(let d = -1; d <= 15; d++){
    const dt = new Date(); dt.setDate(dt.getDate() + d);
    const iso = isoOf(dt);
    if(gateOn && iso > lastReal && !allDays.has(iso)) continue;
    const extra = customForDay(iso);
    if(extra.length){
      byDay[iso] = (byDay[iso] || []).concat(extra);
      allDays.add(iso);
    }
  }

  /*  ② 单次课（指定了具体日期）不受窗口限制 ——
        用户明确填了「某月某日有这节课」，就该在那天出现，
        不管它是不是落在抓取范围内。 */
  for(const c of customLessons()){
    if(!c || !c.once || !c.day) continue;
    if(allDays.has(c.day)) continue;          /* ① 已经收过了 */
    if(!/^\d{4}-\d{2}-\d{2}$/.test(c.day)) continue;
    const extra = customForDay(c.day);
    if(extra.length){
      byDay[c.day] = (byDay[c.day] || []).concat(extra);
      allDays.add(c.day);
    }
  }

  const out = {};
  for(const day of allDays){
    const list = byDay[day] || [];
    /* 只有真的当天有内容（课或自编）才补空档 ——
       否则整个周末都会铺满「自习」，很吵 */
    const realCount = list.filter(l => !l.selfStudy).length;
    out[day] = realCount ? fillDay(list, periods) : list;
  }
  return out;
}


function schedulePage(){
  const sch = DATA.schedule || {};
  const lessons = sch.lessons || [];

  if(!sch.ready){
    return `<div class="emptystate">
      <div class="es-ic">${icon('i-cal','xxl')}</div>
      <div class="es-tt">课表正在准备</div>
      <div class="es-ds">${esc(sch.error || '正在自动登录课表系统……')}</div>
      <button class="es-btn" data-relogin="1">
        ${icon('i-refresh','sm')}立即恢复登录</button>
      <div class="es-note">
        程序会自动尝试登录；如果不行，点上面按钮会弹出登录窗口，<br>
        你只需点一下「登录」即可。
      </div>
    </div>`;
  }

  /*  按日期分组 —— 顺便补空档（没课的节次显示「自习」）
     和合并自编课（上操/社团这类网站没有的）。
     periods 是后端抓的节次时间表；抓不到就退化成原来的行为。 */
  const byDay = completeSchedule(lessons, sch.periods || []);
  const days = Object.keys(byDay).sort();

  const todayISO_ = todayISO();
  const today = byDay[todayISO_] || [];

  let h = '';

  /* ── 下一节课（跨天查找：今天上完就显示下次课） ── */
  const nx = findNextLesson(lessons);
  if(nx){
    const l = nx.lesson;
    const running = nx.running;
    const tmin = minsUntil(l.end);
    const hint = dayHint(l.day);
    /* 起止时间：非今天的课补上日期，避免误以为在今天 */
    const timeTxt = `${hint ? hint + ' ' : ''}${hm(l.start)}${l.end?'–'+hm(l.end):''}`;

    /* 倒计时文案（跨天时用相对日期，不用「67 小时」这种无意义数字） */
    const cdTxt = running
      ? (tmin!=null && tmin>0 ? `${countdownText(tmin)}下课` : '进行中')
      : whenText(l, nx.mins);

    /* 30 分钟内高亮提醒 */
    const urgent = !running && nx.mins!=null && nx.mins<=30
                   && l.day === todayISO();
    const border = running ? 'rgba(76,154,99,.45)'
                 : urgent ? 'rgba(209,122,62,.5)'
                 : 'rgba(74,127,181,.4)';
    const bgGlow = running
      ? 'linear-gradient(150deg,rgba(76,154,99,.15),rgba(76,154,99,.04))'
      : urgent
        ? 'linear-gradient(150deg,rgba(209,122,62,.16),rgba(209,122,62,.04))'
        : 'linear-gradient(150deg,rgba(74,127,181,.13),rgba(139,107,181,.05))';
    const cCol = running ? 'var(--green)' : urgent ? 'var(--orange)' : 'var(--blue)';

    /* 副标题：今天 / 明天 / 周X */
    const whenLabel = running ? '正在上课'
                    : (l.day === todayISO() ? '下一节课'
                       : `下一节课 · ${hint || ''}`);

    h += `<div class="cbox ${urgent?'pulse':''}" style="border-color:${border};
        background:${bgGlow}">
      <div class="ct">${esc(whenLabel)}</div>
      <div style="font-size:18px;font-weight:800;margin:2px 0 6px;
        letter-spacing:-.3px">${esc(l.subject||'(未命名)')}</div>
      <div class="tags">
        <span class="tag" style="color:${cCol};font-weight:700;font-size:11px">
          ${esc(timeTxt)}</span>
        <span class="tag" style="font-size:11px;font-weight:700;
          color:${cCol}"><span class="cdval">${esc(cdTxt)}</span></span>
        ${l.room?`<span class="tag dim">${esc(l.room)}</span>`:''}
        ${l.teacher?`<span class="tag dim">${esc(l.teacher)}</span>`:''}
      </div>
    </div>`;
  }else if(lessons.length){
    h += `<div class="cbox"><div class="ct">下一节课</div>
      <div style="font-size:13px;color:var(--muted);margin-top:4px">
        已抓到的课程范围内没有后续课程</div></div>`;
  }

  /*  自编课表入口 —— 放在课表页顶部，一眼能看到 */
  h += `<div style="display:flex;justify-content:flex-end;margin:0 2px 9px">
    <button class="clbtn" id="clOpen">
      ${icon('i-plus','sm')}自编课</button>
  </div>`;

  /* 今日课程 */
  h += section('sch-today', `${icon('i-pin','sm inl')}今日课程`,
    `<span class="cnt">${today.length}</span>`,
    today.length ? `<div class="vlist">${today.map(l=>lessonCard(l)).join('')}</div>`
                 : '<div class="empty">今天没有课</div>');

  /* 明天课程 */
  const tmr = (()=>{ const d=new Date(); d.setDate(d.getDate()+1);
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; })();
  const tmrList = byDay[tmr] || [];
  h += section('sch-tmr', `${icon('i-sunrise','sm inl')}明日课程`,
    `<span class="cnt">${tmrList.length}</span>`,
    tmrList.length ? `<div class="vlist">${tmrList.map(l=>lessonCard(l)).join('')}</div>`
                   : '<div class="empty">明天没有课</div>');

  /* 后续几天 */
  const rest = days.filter(d=>d>tmr);
  if(rest.length){
    h += section('sch-rest', `${icon('i-cal','sm inl')}后续课程`,
      `<span class="cnt">${rest.length} 天</span>`,
      rest.map(d=>`<div style="margin-bottom:9px">
        <div style="font-size:11px;color:var(--muted);margin:0 0 6px 3px;font-weight:600">
          ${esc(d)}</div>
        <div class="vlist">${(byDay[d]||[]).map(l=>lessonCard(l)).join('')}</div>
      </div>`).join(''));
  }

  if(!lessons.length){
    h = '<div class="empty" style="padding:26px">课表暂无课程数据</div>';
  }
  /*  tab 栏可以横向滚动（4 个 tab 在窄窗口放不下），
     所以切页时把当前 tab 滚进视野 —— 否则用键盘/滑动切到最后一个时，
     用户看不到高亮在哪（体验很困惑）。 */
  try{
    const act = document.querySelector('.tab.active');
    if(act && act.scrollIntoView){
      act.scrollIntoView({block:'nearest', inline:'center',
                          behavior:'smooth'});
    }
  }catch(e){}

  return h + `<div style="height:14px"></div>`;
}

/* ══════════ Tab 切换（带动效） ══════════ */
let curTab = 0;
function switchTab(i, animate){
  curTab = i;
  const pages = $('#pages');
  if(pages) pages.style.transform = `translateX(${-i*100}%)`;

  /* 页面缩放/淡出效果：当前页放大到 1，其他页缩小 */
  document.querySelectorAll('.page').forEach((el, idx)=>{
    el.classList.toggle('on', idx === i);
    el.classList.toggle('off', idx !== i);
  });

  document.querySelectorAll('.tab').forEach(el=>{
    el.classList.toggle('active', +el.dataset.tab === i);
  });
  /*  顶部统计卡只在「课程成绩」页显示 ——
     GPA 页有自己更详细的一整页，不需要再顶一排小卡 */
  const stats = $('#stats');
  if(stats) stats.style.display = (i===0) ? '' : 'none';
  /* 资料页不需要顶部的「数据来源」徽章，避免干扰 */
  const wb = $('#wb');
  if(wb) wb.style.display = (i===0) ? '' : 'none';
  localStorage.setItem('mb.tab', String(i));

  /*  进入「GPA」栏 → 确保渲染一次（数据可能刚刷新） */
  if(i === 2){
    renderGpaPage();
  }

  /*  进入「资料」栏 → 实时刷新当前目录（保证看到最新内容） */
  if(i === 3){
    if(FSTATE.cid){
      filesLoad(FSTATE.fid, false);
    }else{
      const first = (DATA?.courses||[])[0]?.class_id || '';
      if(first){ FSTATE.cid = first; filesLoad('', false); }
    }
  }

  /*  进入「Teams」栏 → 渲染并刷新一次
      （数据可能刚收过，或是首次进来还没有） */
  if(i === 4){
    tmRenderPage();      /* ★ 函数名是 tmRenderPage —— 早先写的
                            renderTeamsPage 从来没定义过，
                            导致切到 Teams 页什么都不渲染 */
    tmLoad();
  }
}

/* ══════════ 下载 ══════════ */
function apiDownloadTask(cid, tid, title){
  watchDownload();
  if(window.pywebview?.api?.download_task)
    return window.pywebview.api.download_task(cid, tid, title);
  if(isWebMode())
    return fetch(`/api/download_task?cid=${encodeURIComponent(cid)}`
      +`&tid=${encodeURIComponent(tid)}&title=${encodeURIComponent(title)}`)
      .then(r=>r.json()).then(d=>{ showDownloadResult(d, title); return d; });
}
function apiDownloadFiles(cid, name){
  watchDownload();
  if(window.pywebview?.api?.download_files)
    return window.pywebview.api.download_files(cid, name);
  if(isWebMode())
    return fetch(`/api/download_files?cid=${encodeURIComponent(cid)}`
      +`&name=${encodeURIComponent(name)}`).then(r=>r.json())
      .then(d=>{ showDownloadResult(d, name); return d; });
}
/* 单个附件直链下载（任务详情面板用） */
function apiDownloadFile(url, filename, subdir){
  watchDownload();
  if(window.pywebview?.api?.download_file)
    return window.pywebview.api.download_file(url, filename, subdir || '');
  if(isWebMode())
    return fetch('/api/download_file?url=' + encodeURIComponent(url)
      + '&name=' + encodeURIComponent(filename))
      .then(r=>r.json()).then(d=>{ showDownloadResult(d, filename); return d; });
  return Promise.resolve({ok:false});
}
function apiOpenFolder(path){
  if(window.pywebview?.api?.open_folder)
    return window.pywebview.api.open_folder(path);
  if(isWebMode()) return fetch('/api/open_folder?path='+encodeURIComponent(path));
}

/* 窗口模式：Python 下载完成后回调这里 */
window.downloadDone = function(ok, total, label, folder, extra, err){
  if(err){
    toast(`下载失败：${err}`, 'err', '');
    return;
  }
  if(ok > 0){
    const sizeTxt = extra ? ` · ${extra}` : '';
    toast(total > 1 ? `已下载 ${ok}/${total} 个文件到「${label}」`
                    : `已下载「${label}」${sizeTxt}`,
          'ok', folder);
  }else{
    toast(`下载未完成（${label}）`, 'err', '');
  }
};

/* 网页模式：统一结果提示 */
function showDownloadResult(d, label){
  toast(d.ok ? `已下载 ${d.count}/${d.total} 个文件到「${label}」`
             : `下载未完成：${d.error||'未知原因'}`,
        d.ok ? 'ok' : 'err', d.folder);
}

/* 轻提示 */
let toastTimer = null;
function toast(text, kind, folder){
  let el = document.getElementById('toast');
  if(!el){
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.className = 'show ' + (kind||'');
  el.innerHTML = `<span>${esc(text)}</span>`
    + (folder ? `<button class="tbtn" id="toastOpen">打开文件夹</button>` : '');
  if(folder){
    const b = document.getElementById('toastOpen');
    if(b) b.onclick = e => { e.stopPropagation(); apiOpenFolder(folder); };
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ el.className = ''; }, 5200);
}

/* ══════════ 下载进度 ══════════

   为什么要有这一块
   ----------------
   所有下载接口都是「起一个后台线程就立刻返回」，所以界面**拿不到任何过程信息**，
   只弹一个 toast 就消失了。用户以为没成功，就再点一次 —— 于是同一个文件被
   下了两遍。实测桌面里出现了 `Biology.pdf` 与 `Biology (1).pdf`，
   **SHA256 完全相同、各 213 MB**，白白多下 203 MB。

   现在这里每 400ms 问一次后端，显示出：
       文件名 · 百分比 · 已下载 · 速度 · 剩余时间
   总大小未知时（响应没有 Content-Length）不显示百分比和剩余时间，
   改用不定长流动条 —— 宁可不显示，也不显示一个瞎跳的假倒计时。

   渲染用「补零宽度」的等宽数字，免得速度数字跳动时整行左右抖。 */

let _dlPollTimer = null;
let _dlSeenRunning = false;
let _dlPollStart = 0;

function fmtBytes(n){
  n = Number(n) || 0;
  if(n < 1024) return Math.round(n) + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while(n >= 1024 && i < u.length - 1);
  return n.toFixed(1) + ' ' + u[i];
}

function fmtEta(sec){
  sec = Math.max(0, Number(sec) || 0);
  if(sec < 60) return Math.round(sec) + ' 秒';
  if(sec < 3600) return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  return Math.floor(sec / 3600) + ' 小时 ' + Math.round((sec % 3600) / 60) + ' 分';
}

function dlEl(){
  let el = document.getElementById('dlprog');
  if(!el){
    el = document.createElement('div');
    el.id = 'dlprog';
    el.innerHTML =
      '<div class="dp-head">'
      + '<span class="dp-idx" id="dlIdx"></span>'
      + '<span class="dp-name" id="dlName">正在准备下载…</span></div>'
      + '<div class="dp-bar"><div class="dp-fill" id="dlFill"></div></div>'
      + '<div class="dp-meta"><span id="dlLeft"></span>'
      + '<span id="dlRight"></span></div>';
    document.body.appendChild(el);
  }
  return el;
}

function dlShow(){ dlEl().classList.add('show'); }
function dlHide(){
  const el = document.getElementById('dlprog');
  if(el) el.classList.remove('show');
}

function apiDownloadProgress(){
  if(window.pywebview?.api?.download_progress)
    return window.pywebview.api.download_progress();
  return Promise.resolve(null);
}

function renderDownloadProgress(s){
  const el = dlEl();
  const total = s.total || 0, idx = s.index || 0;

  const idxEl = document.getElementById('dlIdx');
  if(idxEl) idxEl.textContent = total > 1 ? '(' + idx + '/' + total + ')' : '';
  const nameEl = document.getElementById('dlName');
  if(nameEl) nameEl.textContent = s.name || '正在准备…';

  const pct = (s.percent == null) ? null : Number(s.percent);
  el.classList.toggle('indet', pct == null);
  const fill = document.getElementById('dlFill');
  if(fill && pct != null){
    fill.style.width = Math.max(1, Math.min(100, pct)) + '%';
  }

  const left = document.getElementById('dlLeft');
  const right = document.getElementById('dlRight');

  const a = [];
  if(pct != null) a.push(pct.toFixed(0) + '%');
  a.push('已下载 ' + fmtBytes(s.done));
  if(left) left.textContent = a.join(' · ');

  const b = [];
  if(s.speed > 0) b.push(fmtBytes(s.speed) + '/s');
  if(s.eta) b.push('剩余 ' + fmtEta(s.eta));
  if(right) right.textContent = b.join(' · ');
}

function stopDownloadWatch(){
  if(_dlPollTimer){ clearInterval(_dlPollTimer); _dlPollTimer = null; }
}

/* 任何下载动作开始时调用一次，进度卡就会出现。 */
function watchDownload(){
  _dlSeenRunning = false;
  _dlPollStart = Date.now();
  dlShow();
  if(_dlPollTimer) return;
  _dlPollTimer = setInterval(async ()=>{
    let s = null;
    try{ s = await apiDownloadProgress(); }catch(_){}
    if(!s || !s.running){
      /* 还没见过 running：可能后端还没开始（或文件太小已经结束），
         给 3.5 秒宽限再收起来，避免一闪而过。 */
      if(!_dlSeenRunning && (Date.now() - _dlPollStart) < 3500) return;
      const seen = _dlSeenRunning;
      stopDownloadWatch();
      /* 真下载过就多停 0.9 秒，让人看清最后的 100% */
      setTimeout(dlHide, seen ? 900 : 0);
      return;
    }
    _dlSeenRunning = true;
    renderDownloadProgress(s);
  }, 400);
}

/* ══════════ 晚报 ══════════
   用户要求：每工作日晚 9 点生成晚报，在组件顶部点开，
   包括简单平均的总 GPA、新增作业、新增成绩等等。 */

function apiDigestStatus(){
  if(window.pywebview?.api?.digest_status)
    return window.pywebview.api.digest_status();
  return Promise.resolve({ok:false, unread:false, has_any:false});
}
function apiDigestLatest(){
  if(window.pywebview?.api?.digest_latest)
    return window.pywebview.api.digest_latest();
  return Promise.resolve({ok:false, error:'当前环境不支持晚报'});
}
function apiDigestByDate(d){
  if(window.pywebview?.api?.digest_by_date)
    return window.pywebview.api.digest_by_date(d);
  return Promise.resolve({ok:false, error:'当前环境不支持晚报'});
}
function apiDigestRead(){
  if(window.pywebview?.api?.digest_read)
    return window.pywebview.api.digest_read();
  return Promise.resolve({ok:true});
}

/* 顶栏小红点 */
function refreshDigestDot(){
  apiDigestStatus().then(s=>{
    const dot = $('#digdot'), btn = $('#btnDigest');
    if(!dot || !btn) return;
    const on = !!(s && s.unread);
    dot.classList.toggle('on', on);
    btn.classList.toggle('unread', on);
    if(s && s.has_any && s.date){
      btn.title = `晚报 · 最新 ${s.date}（每工作日晚 9 点生成）`;
    }
  }).catch(()=>{});
}

/* GPA 变化小胶囊 */
function dgDelta(delta){
  if(delta == null) return '';
  if(Math.abs(delta) < 0.05)
    return `<span class="dg-delta flat">${icon('i-minus','sm')}持平</span>`;
  const up = delta > 0;
  return `<span class="dg-delta ${up?'up':'down'}">`
    + `${icon('i-trend','sm')}${up?'+':''}${delta.toFixed(2)}%</span>`;
}

/* 一条晚报条目 */
function dgItem(it, kind){
  const sc = it.score || it.level || '';
  const tags = [];
  if(it.course) tags.push(`<b>${esc(it.course)}</b>`);
  if(it.due_date) tags.push('截止 ' + esc(it.due_date));
  if(kind === 'done') tags.push('<b>已完成</b>');
  if(it.kind) tags.push(esc(it.kind));

  return `<div class="dg-item" data-dgurl="${esc(it.url||'')}">
    <span class="ii">${icon(kind==='grade' ? 'i-award'
      : kind==='done' ? 'i-checkcircle'
      : kind==='due' ? 'i-hourglass'
      : kind==='late' ? 'i-alert'
      : 'i-calplus', 'sm')}</span>
    <span class="ib">
      <span class="it">${esc(it.title||'(未命名)')}</span>
      ${tags.length?`<span class="is">${tags.join('')}</span>`:''}
    </span>
    ${sc?`<span class="ig" style="color:${
      it.level?lvlColor(it.level):pctColor(it.percent)}">${esc(sc)}</span>`:''}
  </div>`;
}

function dgSection(iconName, title, n, list, kind, emptyText){
  let h = `<div class="dg-sec"><div class="shead">
    ${icon(iconName,'sm')}<span>${esc(title)}</span>
    <span class="n">${n}</span></div>`;
  if(list.length){
    h += list.map(it=>dgItem(it, kind)).join('');
  }else{
    h += `<div class="dg-none">${icon(emptyText[0],'lg')}
      <span>${esc(emptyText[1])}</span></div>`;
  }
  return h + `</div>`;
}

/* 渲染整份晚报 */
/* 晚报历史按日期从旧到新排序（后端已排好，这里再兜一次） */
function dgHistorySorted(d){
  const list = ((d && d.history) || [])
    .filter(x => x && x.date)
    .slice();
  list.sort((a, b) => String(a.date).localeCompare(String(b.date)));
  return list;
}

/* 期数导航条：上一期 / 下一期 / 回到最新。
   所有按钮统一用 data-dgdate，由 bindDigest 里的一个处理器接管。 */
function dgNavHTML(d, curDate){
  const hist = dgHistorySorted(d);
  const latest = d.latest_date
    || (hist.length ? hist[hist.length - 1].date : '');
  const cur = curDate || (d && d.date) || '';
  const i = hist.findIndex(x => x.date === cur);
  const prev = i > 0 ? hist[i - 1] : null;
  const next = (i >= 0 && i < hist.length - 1) ? hist[i + 1] : null;
  const isPast = !!latest && cur !== latest;

  let h = `<div class="dg-nav">`;
  h += `<button class="dgnav-btn" data-dgdate="${prev ? esc(prev.date) : ''}"
    ${prev ? '' : 'disabled'} title="${prev ? esc(prev.date) + ' ' + esc(prev.weekday || '') : '没有更早的一期'}">
    ${icon('i-back','sm')}<span>上一期</span></button>`;
  h += `<span class="dgnav-cur">${esc(cur)}`
     + (isPast ? `<span class="past">往期</span>` : '')
     + `</span>`;
  h += `<button class="dgnav-btn" data-dgdate="${next ? esc(next.date) : ''}"
    ${next ? '' : 'disabled'} title="${next ? esc(next.date) + ' ' + esc(next.weekday || '') : '这已经是最新一期'}">
    <span>下一期</span>${icon('i-chevright','sm')}</button>`;
  if(isPast){
    h += `<button class="dgnav-btn latest" data-dgdate="${esc(latest)}"
      title="回到 ${esc(latest)}">${icon('i-mail','sm')}<span>回到最新</span></button>`;
  }
  h += `</div>`;
  return h;
}

function digestRender(d, curDate){
  const c = d.counts || {};
  const g = d.gpa || {};

  const isPast = d.is_latest === false;

  let h = `<div class="dg-head">
    <span class="oi" style="color:var(--blue)">${''}</span>
    <div style="flex:1;min-width:0">
      <div class="t">${icon('i-mail','sm inl')}${isPast ? '往期晚报' : '今日晚报'}</div>
      <div class="d">${esc(d.date||'')} ${esc(d.weekday||'')} · 生成于 ${esc(d.generated||'')}</div>
    </div>
    <button class="dg-x" id="dgClose" title="关闭">${icon('i-x','sm')}</button>
  </div>
  <div class="dg-body">`;

  /* ── 期数导航（常驻）──
     【为什么放最上面且不依赖 history 之外的任何条件】
       「往期晚报」那一行按钮原来只在 history 够长时才渲染，
       而按日期取某一期时后端不带 history —— 点进去就整块消失，
       既回不到最新也换不到别的日期。导航因此必须**独立存在**。 */
  h += dgNavHTML(d, curDate);

  /* ── 总 GPA（简单平均） ── */
  h += `<div class="dg-hero">
    <div>
      <div class="gv">${g.gpa!=null?g.gpa.toFixed(2):'—'}</div>
      <div class="gl">总 GPA（简单平均）</div>
    </div>
    <div style="flex:1" class="gs">
      <div>${g.percent!=null
        ? `<b style="font-size:14px">${esc(g.letter)}</b>
           · ${g.percent.toFixed(2)}% · ${g.count} 门有分数`
        : '还没有任何课程出分'}</div>
      <div style="margin-top:5px">${dgDelta(d.gpa_delta)}</div>
    </div>
  </div>`;

  /* 第一份晚报：没有对比基准，说明一下 */
  if(d.first_run){
    h += `<div class="dg-sec"><div class="dg-none">
      ${icon('i-sparkle','lg')}
      <span>这是第一份晚报。从明天起就能看到「新增了什么」。</span>
    </div></div>`;
  }

  /* ── 四个分组（按用户要求：新增作业 / 新增成绩，另加完成与提醒） ── */
  h += dgSection('i-calplus', '新增作业', c.new_tasks,
    d.new_tasks||[], 'task',
    ['i-inbox','今天没有新增作业，可以歇一会儿']);

  h += dgSection('i-award', '新增成绩', c.new_grades,
    d.new_grades||[], 'grade',
    ['i-inbox','今天还没有出分的作业']);

  h += dgSection('i-checkcircle', '今天完成的', c.new_done,
    d.new_done||[], 'done',
    ['i-inbox','今天还没有完成的作业']);

  h += dgSection('i-hourglass', '今天到期', c.due_soon,
    d.due_soon||[], 'due',
    ['i-check','今天没有到期的作业']);

  if((c.overdue||0) > 0){
    h += dgSection('i-alert', '已经逾期', c.overdue,
      d.overdue||[], 'late', ['i-inbox','没有逾期作业']);
  }

  /* ── 今日概览 ── */
  h += `<div class="dg-sec"><div class="shead">
    ${icon('i-list','sm')}<span>今日概览</span></div>
    <div class="tags" style="padding:2px 0">
      <span class="tag">${esc(c.courses||0)} 门课程</span>
      <span class="tag soon">${esc(c.pending||0)} 项待完成</span>
      ${c.new_tasks?`<span class="tag ok">+${esc(c.new_tasks)} 新作业</span>`:''}
      ${c.new_grades?`<span class="tag grade">+${esc(c.new_grades)} 新成绩</span>`:''}
      ${c.overdue?`<span class="tag due">${esc(c.overdue)} 项逾期</span>`:''}
    </div></div>`;

  /* ── 历史期数（可翻看） ── */
  const hist = dgHistorySorted(d);
  if(hist.length > 1){
    h += `<div class="dg-hist">
      <div class="hh">往期晚报</div>
      <div class="hrow">` + hist.map(x=>{
        const cc = x.counts||{};
        const n = (cc.new_tasks||0) + (cc.new_grades||0);
        return `<button class="hb${x.date===curDate?' cur':''}"
          data-dgdate="${esc(x.date)}"
          title="${esc(x.date)} ${esc(x.weekday||'')} · 新增 ${n} 项"
          >${esc(x.date.slice(5))}${n?` · ${n}`:''}</button>`;
      }).join('') + `</div></div>`;
  }

  h += `<div style="height:8px"></div></div>`;
  return h;
}

function digestShow(data){
  const host = $('#digest'), wrap = $('#digwrap');
  if(!host || !wrap) return;

  if(!data || data.ok === false){
    wrap.innerHTML = `<div class="dg-head">
      <div style="flex:1;min-width:0">
        <div class="t">${icon('i-mail','sm inl')}晚报</div>
        <div class="d">暂不可用</div>
      </div>
      <button class="dg-x" id="dgClose" title="关闭">${icon('i-x','sm')}</button>
    </div>
    <div class="dg-body"><div class="dg-none">
      ${icon('i-inbox','lg')}
      <span>${esc((data&&data.error) || '还没有晚报')}</span>
      <span style="font-size:10.5px;opacity:.8">
        每个工作日晚上 9 点后，打开组件就会自动生成。</span>
    </div></div>`;
  }else{
    wrap.innerHTML = digestRender(data, data.date);
  }

  host.classList.add('open');
  bindDigest(data && data.date);

  /* 打开就算已读 → 清掉小红点 */
  if(data && data.ok !== false){
    apiDigestRead().then(()=>refreshDigestDot()).catch(()=>{});
  }
}

function bindDigest(curDate){
  const host = $('#digest');
  if(!host) return;

  const x = $('#dgClose');
  if(x) x.addEventListener('click', closeDigest);

  /* 点遮罩关闭 */
  host.onclick = (e)=>{ if(e.target === host) closeDigest(); };

  /* 点条目 → 打开原网页 */
  host.querySelectorAll('[data-dgurl]').forEach(el=>{
    el.addEventListener('click', ()=>{
      const u = el.dataset.dgurl;
      if(u) openURL(u);
    });
  });

  /*  翻期：导航条（上一期 / 下一期 / 回到最新）与「往期晚报」
      按钮行走的是同一个处理器 —— 它们都带 data-dgdate。
      禁用的按钮 data-dgdate 为空，下面直接跳过。 */
  host.querySelectorAll('[data-dgdate]').forEach(b=>{
    if(b.dataset.dgbound) return;
    b.dataset.dgbound = '1';
    b.addEventListener('click', e=>{
      e.stopPropagation();
      const want = b.dataset.dgdate;
      if(!want) return;
      if(want === curDate) return;          /* 点的就是当前这一期 */
      if(b.dataset.dgload === '1') return;  /* 防连点：一次只切一期 */
      b.dataset.dgload = '1';
      dgOpenDate(want);
    });
  });
}

/* 切到某一期。失败要说出来 —— 以前失败是静默的，
   用户只会看到「点了没反应」。 */
function dgOpenDate(date){
  if(!date) return;
  apiDigestByDate(date).then(res=>{
    if(!res || res.ok === false){
      toast((res && res.error) || '这一期打不开', 'err', '');
      return;
    }
    const wrap = $('#digwrap');
    if(!wrap) return;
    wrap.innerHTML = digestRender(res, date);
    bindDigest(date);
    /*  切期后回到顶部：否则会停在上一期的滚动位置，
        看起来像「内容没变」。 */
    const body = wrap.querySelector('.dg-body');
    if(body) body.scrollTop = 0;
  }).catch(e=>{
    toast('这一期打不开：' + String(e), 'err', '');
  });
}

function closeDigest(){
  const host = $('#digest');
  if(host) host.classList.remove('open');
}

function openDigest(){
  const host = $('#digest');
  const wrap = $('#digwrap');
  if(host) host.classList.add('open');
  if(wrap){
    wrap.innerHTML = `<div class="dg-head">
      <div style="flex:1"><div class="t">${icon('i-mail','sm inl')}今日晚报</div>
      <div class="d">读取中…</div></div>
      <button class="dg-x" id="dgClose" title="关闭">${icon('i-x','sm')}</button>
    </div><div class="dg-body">
      <div class="fskels">${'<div class="fskel" style="height:56px"></div>'.repeat(4)}</div>
    </div></div>`;
    host.onclick = (e)=>{ if(e.target === host) closeDigest(); };
    const x = $('#dgClose');
    if(x) x.addEventListener('click', closeDigest);
  }
  apiDigestLatest().then(digestShow).catch(e=>{
    digestShow({ok:false, error:String(e)});
  });
}

/* ══════════ 数据与备份 ══════════
   背景：手动完成标记 / 晚报历史 / 成绩历史都只在本机，
        换电脑或重装就没了。这里提供导出/导入。

    设计原则（借鉴 CampusDesk，但完全按 Windows 习惯做）：
     · 导出到**桌面**（不是「文稿」，Windows 用户找桌面最顺）
     · 用**文件夹选择对话框**让用户挑导入文件（Windows 原生体验）
     · 备份里**绝不含**账号密码 / Cookie / token
     · 导入前自动备份当前数据（可以反悔）
   ══════════════════════════════════ */

function apiBackupStatus(){
  if(window.pywebview?.api?.backup_status)
    return window.pywebview.api.backup_status();
  return Promise.resolve({ok:false});
}
function apiBackupExport(){
  if(window.pywebview?.api?.backup_export)
    return window.pywebview.api.backup_export();
  return Promise.resolve({ok:false, error:'当前环境不支持导出'});
}
function apiBackupImport(path){
  if(window.pywebview?.api?.backup_import)
    return window.pywebview.api.backup_import(path);
  return Promise.resolve({ok:false, error:'当前环境不支持导入'});
}

function dataRender(st){
  st = st || {};
  const n = (v) => v==null ? '—' : v;

  return `<div class="dg-head">
    <div style="flex:1;min-width:0">
      <div class="t">${icon('i-shield','sm inl')}数据与备份</div>
      <div class="d">只在本机的学习数据 · 导出后可换电脑继续用</div>
    </div>
    <button class="dg-x" id="dataClose" title="关闭">${icon('i-x','sm')}</button>
  </div>
  <div class="dg-body">

    <!-- ① 当前有多少数据 -->
    <div class="dg-sec"><div class="shead">
      ${icon('i-list','sm')}<span>本机数据</span></div>
      <div class="dgrid">
        <div class="dcell">
          <b>${n(st.manual_done)}</b>
          <span>手动完成标记</span>
          <em>你点过「我已完成」的作业</em>
        </div>
        <div class="dcell">
          <b>${n(st.digests)}</b>
          <span>晚报期数</span>
          <em>每工作日晚 9 点自动生成</em>
        </div>
        <div class="dcell">
          <b>${n(st.grade_history)}</b>
          <span>成绩历史</span>
          <em>只做备份留档，不回写</em>
        </div>
      </div>
    </div>

    <!-- ② 导出 -->
    <div class="dg-sec"><div class="shead">
      ${icon('i-export','sm')}<span>导出备份</span></div>
      <p class="dnote">
        导出一个 JSON 文件到<b>桌面</b>，包含上面这些数据。
      </p>
      <p class="dnote dwarn">
        ${icon('i-shield','sm')} 备份里<b>不包含</b>账号密码、Cookie
        或登录凭据 —— 发给人也安全。
      </p>
      <button class="dbtn primary" id="doExport">
        ${icon('i-export','sm')}导出到桌面</button>
    </div>

    <!-- ③ 导入 -->
    <div class="dg-sec"><div class="shead">
      ${icon('i-import','sm')}<span>导入备份</span></div>
      <p class="dnote">
        从别的电脑导出的 JSON 恢复数据。<br>
        默认<b>合并</b>：不会删掉你现在已有的记录。
      </p>
      <p class="dnote">
        ${icon('i-alert','sm')} 导入前会自动把当前数据备份到
        <code>data / _before_import_&lt;时间&gt;</code>，可以反悔。
      </p>
      <button class="dbtn" id="doImport">
        ${icon('i-import','sm')}选择备份文件…</button>
      <div class="dfiles" id="dfiles"></div>
    </div>

    <div class="dg-hist">
      <div class="hh">说明</div>
      <p class="dnote" style="border:none;padding-top:0">
        刷新缓存、任务详情、附件探测结果都<b>不需要备份</b> ——
        程序会自动重新抓取。只有「你的操作记录」才需要留。
      </p>
    </div>
  </div>`;
}

function bindData(){
  const host = $('#datapanel');
  if(!host) return;

  const x = $('#dataClose');
  if(x) x.addEventListener('click', closeData);
  host.onclick = (e)=>{ if(e.target === host) closeData(); };

  const ex = $('#doExport');
  if(ex) ex.addEventListener('click', ()=>{
    if(ex.dataset.busy) return;
    ex.dataset.busy = '1';
    const old = ex.innerHTML;
    ex.innerHTML = icon('i-refresh','sm') + '<em>正在导出…</em>';
    ex.classList.add('busy');

    apiBackupExport().then(r=>{
      if(r && r.ok){
        const c = r.counts || {};
        toast(`已导出到桌面（标记 ${c.manual_done||0} 条 / `
              + `晚报 ${c.digests||0} 期）`, 'ok', '');
        /*  显示刚导出的文件（不要重绘整个面板，否则这里刚写的会被冲掉） */
        const box = $('#dfiles');
        if(box){
          const full = String(r.path || '');
          /* 同时兼容 Windows 的 \ 和 posix 的 / */
          const name = full.split(/[\\/]/).pop() || full;
          box.innerHTML = `<div class="dfile ok">
            ${icon('i-checkcircle','sm')}
            <span title="${esc(full)}">${esc(name)}</span>
            <em>${((r.bytes||0)/1024).toFixed(1)} KB</em>
          </div>`;
        }
        /* 数字可能变了（比如刚导入过），静默更新一下计数 */
        apiBackupStatus().then(st=>{
          const cells = document.querySelectorAll('#datawrap .dcell b');
          if(cells.length >= 3 && st){
            cells[0].textContent = st.manual_done ?? '—';
            cells[1].textContent = st.digests ?? '—';
            cells[2].textContent = st.grade_history ?? '—';
          }
        }).catch(()=>{});
      }else{
        toast('导出失败：' + ((r&&r.error) || '未知原因'), 'err', '');
      }
    }).catch(e=> toast('导出失败：' + e, 'err', ''))
      .finally(()=>{
        delete ex.dataset.busy;
        ex.classList.remove('busy');
        ex.innerHTML = old;
      });
  });

  const im = $('#doImport');
  if(im) im.addEventListener('click', ()=>{
    if(im.dataset.busy) return;
    /*  Windows 习惯：用原生文件选择对话框 */
    if(window.pywebview?.api?.pick_backup_file){
      im.dataset.busy = '1';
      window.pywebview.api.pick_backup_file().then(path=>{
        delete im.dataset.busy;
        if(!path) return;                     /* 用户取消了 */
        doImport(path);
      }).catch(e=>{
        delete im.dataset.busy;
        toast('打开文件选择器失败：' + e, 'err', '');
      });
    }else{
      const f = $('#backupFile');
      if(f) f.click();
    }
  });

  /* 网页模式下的文件选择（没有原生对话框时） */
  const f = $('#backupFile');
  if(f && !f.dataset.bound){
    f.dataset.bound = '1';
    f.addEventListener('change', ()=>{
      const file = f.files && f.files[0];
      if(!file) return;
      const rd = new FileReader();
      rd.onload = ()=>{
        try{
          const payload = JSON.parse(rd.result);
          /* 网页模式下没有后端路径，只能提示用桌面版 */
          toast('网页模式请用桌面版导入（需要文件路径）', 'err', '');
        }catch(e){
          toast('文件不是有效的 JSON', 'err', '');
        }
      };
      rd.readAsText(file);
      f.value = '';
    });
  }
}

function doImport(path){
  /*  兼容 Windows 的 \ 和 posix 的 / */
  const name = String(path).split(/[\\/]/).pop() || String(path);
  if(!confirm('确定导入「' + name + '」吗？\n\n'
    + '· 默认合并，不会删掉现有记录\n'
    + '· 导入前会自动备份当前数据')) return;

  const im = $('#doImport');
  const old = im ? im.innerHTML : '';
  if(im){
    im.dataset.busy = '1';
    im.classList.add('busy');
    im.innerHTML = icon('i-refresh','sm') + '<em>正在导入…</em>';
  }
  toast('正在导入…', '', '');

  apiBackupImport(path).then(r=>{
    if(r && r.ok){
      toast('导入成功：标记 ' + (r.manual_done||0) + ' 条 / '
            + '晚报 ' + (r.digests||0) + ' 期', 'ok', '');
      /* 数据变了 → 强制重绘 */
      lastSig = '';
      poll();
      /*  只更新计数，不整个重绘（免得刚写的东西被冲掉） */
      const cells = document.querySelectorAll('#datawrap .dcell b');
      if(cells.length >= 3){
        cells[0].textContent = (r.manual_done != null) ? r.manual_done : '—';
        cells[1].textContent = (r.digests != null) ? r.digests : '—';
        cells[2].textContent = (r.grade_history != null) ? r.grade_history : '—';
      }
      const box = $('#dfiles');
      if(box){
        box.innerHTML = '<div class="dfile ok">'
          + icon('i-checkcircle','sm')
          + '<span title="' + esc(String(path)) + '">' + esc(name) + '</span>'
          + '<em>已导入</em></div>';
      }
    }else{
      toast('导入失败：' + ((r&&r.error) || '未知原因'), 'err', '');
    }
  }).catch(e=> toast('导入失败：' + e, 'err', ''))
    .finally(()=>{
      if(im){
        delete im.dataset.busy;
        im.classList.remove('busy');
        im.innerHTML = old;
      }
    });
}

function closeData(){
  const host = $('#datapanel');
  if(host) host.classList.remove('open');
}

function openData(){
  const host = $('#datapanel');
  const wrap = $('#datawrap');
  if(!host || !wrap) return;
  host.classList.add('open');
  wrap.innerHTML = `<div class="dg-head">
      <div style="flex:1"><div class="t">${icon('i-shield','sm inl')}数据与备份</div>
      <div class="d">读取中…</div></div>
      <button class="dg-x" id="dataClose" title="关闭">${icon('i-x','sm')}</button>
    </div><div class="dg-body">
      <div class="fskels">${'<div class="fskel" style="height:52px"></div>'.repeat(3)}</div>
    </div></div>`;
  host.onclick = (e)=>{ if(e.target === host) closeData(); };
  const x = $('#dataClose');
  if(x) x.addEventListener('click', closeData);

  apiBackupStatus().then(st=>{
    wrap.innerHTML = dataRender(st);
    bindData();
  }).catch(()=>{
    wrap.innerHTML = `<div class="dg-head">
      <div style="flex:1"><div class="t">数据与备份</div></div>
      <button class="dg-x" id="dataClose">${icon('i-x','sm')}</button>
    </div><div class="dg-body"><div class="dg-none">
      ${icon('i-alert','lg')}<span>读取失败</span></div></div>`;
    const x2 = $('#dataClose');
    if(x2) x2.addEventListener('click', closeData);
  });
}

/* ══════════ 空状态（避免界面空白） ══════════ */

/*  把「最近一次尝试」与「最近一次有内容」两个时间摆出来。
    这两个时间不一样时，用户才看得出「现在看的是旧数据」。 */
function attemptLine(){
  const at = DATA.last_attempt_at || '';
  const ok = DATA.last_attempt_ok !== false;
  const err = DATA.last_attempt_error || '';
  const up = DATA.updated || '';
  if(!at) return '';
  const hm = s => (s || '').slice(11, 16) || (s || '').slice(0, 16);
  if(ok){
    return up ? `本次读取：${esc(hm(at))}` : '';
  }
  let line = `本次读取未成功`;
  if(at) line += `（${esc(hm(at))}）`;
  if(err) line += `：${esc(err)}`;
  if(up) line += `  ·  当前显示的是 ${esc(up)} 的数据`;
  return line;
}

function emptyState(){
  const st = DATA.status || '';
  let iconName = 'i-inbox', title = '暂无数据', desc = '', btn = '';

  /*  第一次用（还没填过账号）—— 这是最常见的状态，
     引导语要明确告诉他「去填一次就好」，而不是说「登录过期」。 */
  if(!HAS_CREDENTIALS){
    iconName = 'i-key';
    title = '先填一次账号';
    desc = '填好之后程序会自己登录、自己刷新，以后不用再管。';
    btn = `<button class="es-btn" data-opensetup="1">
      ${icon('i-key','sm')}填写账号</button>`;
  }else if(st === 'need_login'){
    iconName = 'i-key';
    title = '登录状态已过期';
    desc = DATA.message
      || '程序正在自动重新登录，稍等一会儿就好。';
    btn = `<button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}立即重新登录</button>`;
  }else if(st === 'unread'){
    /*  本次没读到。与「本来就没内容」分开说 ——
        不然用户看到「没有待办」不知道能不能信。 */
    iconName = 'i-alert';
    title = '本次没读取成功';
    desc = attemptLine() || DATA.message || '稍后会自动重试。';
    btn = `<button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}立即重试</button>`;
  }else if(st === 'partial'){
    /*  少数课程没读到、其余已更新。以前后端会发这个态，但这里没有分支，
        于是它落进最后那条兜底 —— 用户只看到「点右上角刷新试试」，
        看不出「有些课没取到」这件事。 */
    iconName = 'i-alert';
    title = '部分课程没读到';
    desc = DATA.message || '注意：有课程未取到，其余已更新。';
    btn = `<button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}重新读取</button>`;
  }else if(st === 'empty'){
    iconName = 'i-checkcircle';
    title = '本次读取成功：没有内容';
    desc = DATA.message
      || '这不是出错 —— 课程都读到了，只是没有待办。';
  }else if(st === 'running'){
    iconName = 'i-hourglass';
    title = '正在读取数据……';
    desc = '正在登录并读取所有课程，约 20 秒。';
  }else{
    /*  兜底。后端目前产生的态是
        idle / running / ok / empty / partial / unread / need_login 七种，
        都已在上面分流；走到这里说明出现了没预料到的态。 */
    desc = '点右上角刷新试试。';
  }

  return `<div class="emptystate">
    <div class="es-ic">${icon(iconName, 'xxl')}</div>
    <div class="es-tt">${esc(title)}</div>
    <div class="es-ds">${esc(desc)}</div>
    ${btn}
    ${(!HAS_CREDENTIALS || st==='need_login')?`<div class="es-note">
      点上面的按钮可以一键恢复。程序平时也会自动重试，不用你管。
    </div>`:''}
  </div>`;
}

/* ══════════ 数据来源徽章（体现「打开即有内容」） ══════════ */
/*  徽章只在「文字真的变了」时才重建 DOM。
   之前 render() 每秒无条件 `innerHTML = sourceBadge()`，
   而 .wbadge 上挂着入场动画 —— 结果动画每秒重播一次，
   看起来像「一直在滚」。现在加两重保护：
     ① 文字签名比对，没变就直接 return（不动 DOM）
     ② 真变了才播一次弹入动画（Web Animations API，
        不会因为 class 重设而重播）                                */
let lastBadgeSig = '';
function renderWarmBadge(){
  const wb = $('#wb');
  if(!wb) return;
  const html = sourceBadge();
  if(html === lastBadgeSig) return;      /* ① 没变 → 一个字都不动 */
  lastBadgeSig = html;
  wb.innerHTML = html;
  if(!html) return;
  const el = wb.firstElementChild;
  /* ② 只在「首次出现」或「状态切换」时弹一下，幅度很小 */
  if(el && el.animate){
    el.animate([{opacity:0, transform:'translateY(4px)'},
       {opacity:1, transform:'none'}],
      {duration:220, easing:'cubic-bezier(.22,1,.36,1)'}
    );
  }
}

function sourceBadge(){
  const w = WARM || {};
  const age = w.cache_age;
  if(age === undefined || age === null) return '';

  /*  时间粒度要「粗」—— 文案只在跨过整分钟/整小时时才变，
     这样徽章不会每隔几秒就重新渲染一次文字。 */
  let txt, kind;
  if(age < 300){                       /* < 5 分钟 */
    txt = '刚刚自动更新'; kind = 'fresh';
  }else if(age < 3600){
    txt = `${Math.floor(age/60)} 分钟前自动更新`; kind = 'fresh';
  }else if(age < 86400){
    txt = `${Math.floor(age/3600)} 小时前更新`; kind = 'stale';
  }else{
    txt = `${Math.floor(age/86400)} 天前更新`; kind = 'stale';
  }

  if(w.reason === 'cache_fresh'){
    txt = `${txt} · 开机已预热`;
  }
  return `<span class="wbadge ${kind}" title="${esc(w.message||'')}"
    >${icon('i-zap','sm')}${esc(txt)}</span>`;
}

function scheduleEmptyState(){
  const sch = DATA.schedule || {};
  return `<div class="emptystate">
    <div class="es-ic">${icon('i-cal','xxl')}</div>
    <div class="es-tt">课表正在准备</div>
    <div class="es-ds">${esc(sch.error || '正在自动登录课表系统……')}</div>
    <button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}立即恢复登录</button>
    <div class="es-note">
      程序会自动尝试；如果不行，点上面按钮会弹出登录窗口，<br>
      你只需点一下「登录」即可。
    </div>
  </div>`;
}

/* ══════════ 每秒轻量更新倒计时 ══════════
   render() 里有签名判断（数据没变就不重建 DOM），
   所以倒计时不能靠 render 刷新 —— 这里单独每秒更新那个数字，
   既保证「几秒内实时」，又不会打断滚动/折叠。 */
let lastCdText = '';
function tickCountdown(){
  const el = document.querySelector('#page-sch .cdval');
  if(!el) return;
  /*  课表页不可见时跳过（省 CPU）。
      ⚠ 判定用 curTab 而不是 offsetParent —— 页面是并排横移的，
      `.page.off` 只改 opacity/scale，offsetParent 永远不为 null，
      用它会白算一遍（之前的判断从来没生效过）。 */
  if(curTab !== 1) return;
  if(!DATA) return;

  const lessons = (DATA.schedule||{}).lessons || [];
  const nx = findNextLesson(lessons);
  if(!nx) return;

  const l = nx.lesson;
  const tmin = minsUntil(l.end);
  const txt = nx.running
    ? (tmin!=null && tmin>0 ? `${countdownText(tmin)}下课` : '进行中')
    : whenText(l, nx.mins);

  if(txt !== lastCdText){
    lastCdText = txt;
    el.textContent = txt;
    el.classList.remove('cdtick');
    void el.offsetWidth;                  /* 触发重排以重播动画 */
    el.classList.add('cdtick');
  }

  /* 上课开始/结束 → 需要完整重绘（颜色、标题、徽章都会变） */
  const title = el.closest('.cbox')?.querySelector('.ct')?.textContent || '';
  if(nx.running !== title.includes('正在上课')) scheduleDirty = true;
}

/* ══════════ 主渲染 ══════════ */
let lastSig = '';
let lastEmptySig = '';      /* 空状态的签名（避免每秒重建） */
let lastStatsSig = '';      /* 统计卡的签名 */
let lastFilesSig = '';      /* 资料页的课程列表签名（只在课程变动时重建） */

function render(){
  if(!DATA) return;

  /*  ★ 版本号以数据为准 ★
      早先在 index.html 里写死 `v0.5.2`，每次升级都得记得手改，
      漏了就会在界面上显示旧版本。 */
  try{
    const vEl = document.querySelector('.ver');
    if(vEl && DATA.version) vEl.textContent = 'v' + DATA.version;
  }catch(_){}

  /*  每次渲染都用当前时间重算剩余天数 —— 数据不会随时间变旧 */
  const tasks = (DATA.tasks||[]).map(liveTask);
  const courses = DATA.courses||[];
  renderStats(tasks);

  /* 刷新状态条
      这里的文字**每秒都在变**（倒计时），所以：
        ① CSS 里不能有入场动画（否则每秒重播）
        ② className / textContent 只在真的变了才写
           —— 无脑重设会触发样式重算和重排 */
  const msg = $('#msg');
  const cd = DATA.next_refresh_in;
  let mtext = DATA.message || '';
  /*  状态条是用户首先看到的地方。
      本次没读到（unread）时，把「什么时候试过、看的是什么时候的数据」
      排在前面 —— 否则「数据没变」看起来像程序卡了。 */
  if(DATA.status === 'unread' && DATA.updated){
    const at = (DATA.last_attempt_at || '').slice(11, 16);
    mtext = (at ? `${at} 读取未成功 · ` : '') + DATA.message
          + `  ·  显示 ${DATA.updated} 的数据`;
  }
  if(DATA.auto_refresh){
    const auto = cd>0 ? `· ${fmtCountdown(cd)}后自动刷新` : '· 正在刷新…';
    mtext = mtext ? `${mtext}  ${auto}` : auto;
  }
  const mcls = mtext
    ? 'show ' + (DATA.status==='need_login'||DATA.status==='running'
        ? 'warn' : (DATA.status==='unread'||DATA.status==='partial') ? 'err' : '')
    : '';
  /*  ★ 状态文字单独放在一个 <span> 里 ——
      因为 #msg 里还多了一个「立即刷新」按钮。
      直接写 msg.textContent 会把那个按钮**一起抹掉**
      （textContent 赋值 = 清空所有子节点）。
      这是个很容易犯的错，之前「改按钮时把里面 SVG 抹掉」也是同源问题。 */
  let tx = msg.querySelector('.tx');
  if(!tx){
    tx = document.createElement('span');
    tx.className = 'tx';
    msg.insertBefore(tx, msg.firstChild);
  }
  if(tx.textContent !== mtext) tx.textContent = mtext;
  if(msg.className !== mcls) msg.className = mcls;

  /*  ── 重试键：失败时才出现，成功自动隐藏 ──
      用户要求：「直到刷新成功再隐藏」。
      ★ 这里只切换 busy 类，**不隐藏** ——
        刷新过程中状态会变成 running（不再是 err/warn），
        按钮自然就藏起来了。所以不用在这里手动控制显隐，
        交给上面那条 CSS 规则（#msg.err / #msg.warn）即可。 */
  const rt = msg.querySelector('#msgRetry');
  if(rt){
    const busy = DATA.status === 'running';
    if(rt.classList.contains('busy') !== busy){
      rt.classList.toggle('busy', busy);
    }
  }

  const updTxt = DATA.updated || '未更新';
  const updEl = $('#upd');
  if(updEl.textContent !== updTxt) updEl.textContent = updTxt;
  /*  ★ 用 getElementById 拿元素再判空 ——
      这个按钮已经从标题栏挪到设置面板，
      而设置面板在文件更后面，`$('#btnRefresh')` 可能拿到 null。
      直接调 .classList 会整个脚本崩掉（页面白屏）。 */
  const _rf = document.getElementById('btnRefresh') ||
              document.getElementById('opRefresh');
  if(_rf) _rf.classList.toggle('spin', DATA.status==='running');

  /* 数据来源徽章（开机预热的成果）
      必须走 renderWarmBadge —— 它内部比对文字签名，
        没变就不碰 DOM，否则入场动画会每秒重播。 */
  renderWarmBadge();

  /* 倒计时 + 自动刷新开关状态
      这里的数字每秒都变 —— className 和 textContent 都要先比对，
        无脑重设会让按钮的 hover/active 状态被反复打断。 */
  const dot = $('#live');
  if(dot){
    const dtxt = DATA.auto_refresh ? (cd>0?fmtCountdown(cd):'') : '||';
    const dcls = 'ibtn' + (DATA.auto_refresh ? ' on':'');
    if(dot.textContent !== dtxt) dot.textContent = dtxt;
    if(dot.className !== dcls) dot.className = dcls;
    dot.title = DATA.auto_refresh
      ? `每 ${(DATA.interval_text || '5 分钟')}自动刷新，还剩 ${fmtCountdown(cd)}`
      : '自动刷新已暂停';
  }
  /*  自动刷新开关状态 —— 0.5.1 起开关本体在设置面板（opAuto），
      标题栏只留 #live 倒计时。老 id 留着兜底，拿不到就跳过。 */
  const ba = document.getElementById('opAuto') ||
             document.getElementById('btnAuto');
  if(ba){
    /*  不能整个刷 textContent —— 那样会把里面的 <svg> 图标抹掉。
       换 <use> 的 href、再按变化更新文字即可。 */
    const u = ba.querySelector('use');
    if(u) u.setAttribute('href', DATA.auto_refresh ? '#i-pause' : '#i-play');
    ba.classList.toggle('paused', !DATA.auto_refresh);
    const _atxt = DATA.auto_refresh ? '暂停自动刷新' : '开启自动刷新';
    const _asp = ba.querySelector('span');
    if(_asp && _asp.textContent !== _atxt) _asp.textContent = _atxt;
    if(ba.title !== _atxt) ba.title = _atxt;
  }

  if(!tasks.length && !courses.length){
    /* 没有任何数据 → 显示明确的原因与操作指引，绝不空白。
       ★ 必须先比对签名再写 DOM —— 这段每秒都会执行，
         无条件重建会让里面的入场动画每秒重播（看起来像在闪）。 */
    const eSig = JSON.stringify([
      DATA.status, DATA.message, DATA.login_error,
      DATA.schedule && DATA.schedule.ready,
      DATA.schedule && DATA.schedule.error,
      HAS_CREDENTIALS,
    ]);
    if(eSig !== lastEmptySig){
      lastEmptySig = eSig;
      $('#page-grade').innerHTML = emptyState();
      $('#page-sch').innerHTML = DATA.schedule && DATA.schedule.ready
        ? schedulePage() : scheduleEmptyState();
      bindRelogin(document);
      bindOpenSetup(document);
    }

    /* 即使成绩抓不到，课表也要能正常显示计数 */
    const bt0 = $('#badge-tasks'), bs0 = $('#badge-sch');
    if(bt0) bt0.textContent = '—';
    if(bs0){
      const sch = DATA.schedule||{};
      if(sch.ready){
        const tISO = todayISO();
        const leftCnt = (sch.lessons||[]).filter(l =>
          l.day===tISO && (minsUntil(l.end)||0) > 0).length;
        bs0.textContent = leftCnt || (sch.lessons||[]).length;
      }else{
        bs0.textContent = '—';
      }
    }
    lastSig = '';
    return;
  }

  if(DATA.status==='running' && !tasks.length && !courses.length){
    /* 骨架屏同样要防重播 */
    if(lastEmptySig !== '__skeleton__'){
      lastEmptySig = '__skeleton__';
      $('#page-grade').innerHTML =
        '<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
    }
    lastSig = '';
    return;
  }

  /*  已完成的任务不再提醒 —— 只统计「未完成」的 */
  const todo = tasks.filter(t => !t.completed);
  const done = tasks.filter(t => t.completed);

  const overdue = todo.filter(t=>t.days_left!=null && t.days_left<0)
    .sort((a,b)=>a.days_left-b.days_left);
  const today = todo.filter(t=>t.days_left===0);
  const soon = todo.filter(t=>t.days_left!=null && t.days_left>0 && t.days_left<=7)
    .sort((a,b)=>a.days_left-b.days_left);
  const later = todo.filter(t=>t.days_left==null || t.days_left>7)
    .sort((a,b)=>(a.days_left??9999)-(b.days_left??9999));

  /* 合并所有待办（供后续分组与签名比较） */
  const pendingAll = [...overdue, ...today, ...soon, ...later];

  /* 只在「数据分组」变化时重建 DOM，避免打断用户滚动/折叠 */
  /*  ══════════ ★ 签名必须包含成绩（0.5.2 修）══════════
   *
   *  【原来为什么坏】
   *    签名里只有 URL 和剩余天数，**没有成绩**：
   *
   *        pendingAll.map(t => t.url + '|' + t.days_left)
   *
   *    于是老师给分之后：
   *      · 任务 URL 没变
   *      · 剩余天数没变（同一天里看的话）
   *      → 签名完全相同 → 下面 `if(sig === lastSig) return;`
   *        直接跳过整页重绘
   *      → 卡片上的成绩标签、GPA 页的数字**全都停在旧值**
   *
   *    用户看到的就是「新获得了成绩，但 GPA 和 tag 没更新」。
   *
   *  【修法】
   *    把「会影响显示」的字段都算进去：成绩、等级、百分比、已评分、
   *    分类、完成状态。改过成绩就一定会触发重绘。
   *
   *  ★ 只加真正影响显示的字段，不要把整个对象 stringify ——
   *    那会让签名每次都变，每 5 分钟自动刷新都重绘一遍，
   *    打断用户正在看的折叠/滚动状态（这个坑在 0.5.1 踩过：
   *    主签名一变就重建，把用户正浏览的资料页打回去了）。
   */
  const gradeKey = t =>
    [t.score, t.level, t.percent, t.graded ? 1 : 0, t.category,
     t.completed ? 1 : 0, t.submitted ? 1 : 0].join('~');

  /*  「今日新获得」台账的指纹。
   *
   *  只取「会影响显示」的字段：条目标题 + 成绩 + 数量。
   *  ★ 不要整个 JSON.stringify —— 台账里有 days_left 这种每天都变的，
   *    那样签名每次都会不同，界面每 5 分钟无条件重绘，
   *    把用户正在看的折叠/滚动状态打断。
   */
  const _tnKey = tn => {
    if(!tn) return '';
    const g = (tn.grades || []).map(x =>
      [x.title, x.score, x.level, x.percent].join('~'));
    const k = (tn.tasks || []).map(x => [x.title, x.due_date].join('~'));
    return [g.length, k.length, g.join(';'), k.join(';')].join('|');
  };

  const sig = JSON.stringify([
    pendingAll.map(t => t.url + '|' + t.days_left + '|' + gradeKey(t)),
    done.map(t => t.url + '|' + gradeKey(t)),
    done.length,
    DATA.updated, DATA.status, collapsed, openCourses,
    (DATA.schedule||{}).updated, scheduleDirty,
    /*  课程总评：GPA 页与「课程与成绩」区块直接读它，
        改了总评也必须重绘。 */
    courses.map(c => c.class_id + ':' + (c.overall_percent ?? '')
                    + '/' + (c.overall_level ?? '')).join(','),
    /*  分类成绩（Task Category Averages）—— GPA 页表格用 */
    courses.map(c => (c.categories||[])
              .map(x => (x.name||'') + ':' + (x.percent ?? '')
                        + '/' + (x.graded ? 1 : 0)).join(';')).join('|'),
    /*  ★ 「今日新获得」台账（0.5.2 修）
     *
     *  这一块读的是 DATA.today_new，**不是** DATA.tasks ——
     *  两者是独立的数据源。老师给分之后：
     *    · DATA.tasks 更新了（上面 gradeKey 能感知）
     *    · 但「今日新获得」显示的是台账里那条 ——
     *      以前台账只在「第一次出分」时记一次，改分不更新，
     *      于是这块永远显示第一次那个分数。
     *  后端修了（today_new._grade_changed），这里也要把它纳入签名，
     *  否则台账更新了界面照样不重绘。
     */
    _tnKey(DATA.today_new),
  ]);
  if(sig === lastSig) return;
  lastSig = sig;
  scheduleDirty = false;

  /* ── 第 1 页：课程成绩（待办 / 测验 / 作业分类） ── */
  let h = '';

  /*  重建前先把阅读状态存下来（滚动位置 / 局部滚动），
      重建后放回。理由见 saveReading 的说明。 */
  const _readPG = saveReading($('#page-grade'));

  const quizList = pendingAll.filter(isQuiz);
  const hwList = pendingAll.filter(t => !isQuiz(t) && isHomework(t));
  const otherList = pendingAll.filter(t => !isQuiz(t) && !isHomework(t));

  const urgentCnt = t => t.days_left != null && t.days_left <= 3;
  const urgent = pendingAll.filter(urgentCnt).length;

  /*  「我已完成」按钮的显示策略（用户要求）：
       有紧急待办（3 天内 / 已过期）→ 显示，方便快速清理
       完全没有紧急待办          → 自动隐藏，界面更干净
       已经标记过的卡片始终显示「撤销」，否则无法反悔。 */
  const showMark = urgent > 0;

  h += section('tasks',
    `${icon('i-pin','sm inl')}全部待办`,
    urgent ? `<span class="warn">${urgent} 项紧急</span>`
           : `<span class="cnt">${pendingAll.length}</span>`,
    taskCards(pendingAll, {showMark}) || '<div class="empty">全部完成</div>');

  /*  ── 今日新获得（用户要求：紧随待办之后）──
       数据来自 DATA.today_new（后端按天累积的台账）。
       空的时候 todayNewBlock() 返回空串，界面上完全不出现。 */
  h += todayNewBlock();

  /*  Quiz 独立列表 */
  if(quizList.length){
    const qUrgent = quizList.filter(urgentCnt).length;
    h += section('quiz', `${icon('i-flask','sm inl')}测验 Quiz`,
      qUrgent ? `<span class="warn">${qUrgent} 项紧急</span>`
              : `<span class="cnt">${quizList.length}</span>`,
      taskCards(quizList, {showMark}));
  }

  /* 作业独立列表 */
  if(hwList.length){
    const hUrgent = hwList.filter(urgentCnt).length;
    h += section('hw', `${icon('i-note','sm inl')}作业 Homework`,
      hUrgent ? `<span class="warn">${hUrgent} 项紧急</span>`
              : `<span class="cnt">${hwList.length}</span>`,
      taskCards(hwList, {showMark}));
  }

  /* 其他 */
  if(otherList.length){
    h += section('other', `${icon('i-list','sm inl')}其他任务`,
      `<span class="cnt">${otherList.length}</span>`,
      taskCards(otherList, {showMark}));
  }

  h += section('courses', `${icon('i-grades','sm inl')}课程与成绩`,
    `<span class="cnt">${courses.length} 门</span>`,
    courses.map((c,i)=>courseBlock(c,i)).join('') || '<div class="empty">暂无课程</div>');

  if(done.length) h += section('done', `${icon('i-checkcircle','sm inl')}已完成`,
    `<span class="cnt">${done.length}</span>`,
    taskCards(done, {showMark}));

  $('#page-grade').innerHTML = h;
  restoreReading($('#page-grade'), _readPG);

  /* ── 第 2 页：今日课表 ── */
  const _readSCH = saveReading($('#page-sch'));
  $('#page-sch').innerHTML = schedulePage();
  restoreReading($('#page-sch'), _readSCH);

  /* ── 第 3 页：资料（实时浏览 ManageBac Files） ──

     ★ 只在「课程列表变了」时才重建这一页。
       以前主签名一变就重建（某个任务的剩余天数从 3 变 2 也算），
       而资料页的内容完全由 FSTATE + 用户操作驱动，
       跟任务列表没关系 —— 重建只会把用户正浏览的文件夹打回去。 */
  const filesSig = courses.map(c=>c.class_id).join(',');
  if(FSTATE.cid){
    if(filesSig !== lastFilesSig){
      lastFilesSig = filesSig;
      $('#page-files').innerHTML = filesPage();
      bindFiles();
    }
  }else{
    /* 首次进入：选第一门课并拉取 */
    lastFilesSig = filesSig;
    const first = (courses[0]?.class_id) || '';
    if(first){
      FSTATE.cid = first;
      filesLoad('', false);
    }else{
      $('#page-files').innerHTML = `<div class="fempty">
        <div class="fi">${icon('i-folder')}</div><div class="ft">暂无课程资料</div>
        <div class="fd">还没有抓到课程。</div></div>`;
    }
  }

  /* 页签计数 */
  const bt = $('#badge-tasks'), bs = $('#badge-sch');
  if(bt) bt.textContent = todo.length;
  /*  Teams 徽章：显示已同步的消息条数。
      没同步过（还没连 Teams）就显示 —，不显示 0 ——
      0 会让人以为「同步过了但没消息」。 */
  const btm = $('#badge-tm');
  if(btm && Array.isArray(TM_ITEMS)){
    if(TM_ITEMS.length){
      btm.textContent = TM_ITEMS.length > 99 ? '99+' : TM_ITEMS.length;
      btm.title = `已同步 ${TM_ITEMS.length} 条消息`;
    }else{
      const st = TM_STATUS_CACHE || {};
      btm.textContent = st.ready ? '0' : '—';
      btm.title = st.ready ? '同步过，但没有消息' : '还没同步过 Teams';
    }
  }
  if(bs){
    const sch = DATA.schedule||{};
    if(sch.ready){
      /* 显示「今天还剩几节」，比总节数有用得多 */
      const tISO = todayISO();
      const leftCnt = (sch.lessons||[]).filter(l =>
        l.day === tISO && (minsUntil(l.end)||0) > 0).length;
      bs.textContent = leftCnt || (sch.lessons||[]).length;
      bs.title = leftCnt ? `今天还有 ${leftCnt} 节` : '今天的课已结束';
    }else{
      bs.textContent = '—';
    }
  }
  /*  GPA 页签：显示「有总评的课程数」。
      0.5.1 之前忘了写这个徽章，一直是个破折号。 */
  const bg = $('#badge-gpa');
  if(bg){
    const nGpa = courses.filter(c => c.overall_percent != null).length;
    bg.textContent = nGpa || '—';
    bg.title = nGpa ? `${nGpa} 门课有总评` : '还没有出总评的课程';
  }

  /*  GPA 页是切进去时才渲染的；数据刷新后如果正停在这一页，
      重绘一次，免得看到上一轮的数字。 */
  if(curTab === 2) renderGpaPage();

  bind();
  /*  空状态里的「修复」按钮也要绑定（它们不在 bind() 的扫描范围内） */
  bindRelogin(document);
  /*  刚长出来的卡片还没贴液态玻璃的类（.lq / .lq-eff）。
      lqMark / lqFlow 本身幂等，已贴过的不重复。 */
  try{ lqFlow(document.body); }catch(e){}
}

/* ══════════ 「我已完成，不再提示」 ══════════
   用途：线下交了纸质版、老师没给成绩、线上也没有提交徽章的任务。
        按唯一完成规则会一直挂在待办里碍眼，这里允许手动标记。
    只存本机，不会写入 ManageBac，不影响学校系统。 */

/* 找到本地缓存的任务对象（用于取 title / course） */
/* 在 DATA 里按 URL 反查任务。
 *
 *  ★ 要比对「去掉 # 片段」之后的 URL：
 *    ManageBac 的同一个任务在不同页面可能带不同的锚点，
 *    只做全等比较会漏掉，于是 dpPreferListGrade 拿不到最新成绩。
 *    （0.5.2 修成绩不更新时发现的）
 */
function findTaskByUrl(url){
  const all = [...(DATA?.tasks||[])];
  (DATA?.courses||[]).forEach(c => (c.tasks||[]).forEach(t => all.push(t)));
  const norm = u => String(u || '').split('#')[0];
  const target = norm(url);
  return all.find(t => t.url === url)            /* 先试全等（最快） */
      || all.find(t => norm(t.url) === target)   /* 再试去锚点 */
      || null;
}

function apiMarkDone(url, title, course){
  if(window.pywebview?.api?.mark_done)
    return window.pywebview.api.mark_done(url, title, course);
  if(isWebMode())
    return fetch('/api/mark_done?url=' + encodeURIComponent(url)
      + '&title=' + encodeURIComponent(title)
      + '&course=' + encodeURIComponent(course)).then(r=>r.json());
  /* 纯静态预览：只改内存，刷新即失效 */
  return Promise.resolve({ok:true, local:true});
}
function apiUnmarkDone(url, title, course){
  if(window.pywebview?.api?.unmark_done)
    return window.pywebview.api.unmark_done(url, title, course);
  if(isWebMode())
    return fetch('/api/unmark_done?url=' + encodeURIComponent(url)
      + '&title=' + encodeURIComponent(title)
      + '&course=' + encodeURIComponent(course)).then(r=>r.json());
  return Promise.resolve({ok:true, local:true});
}

/* 立即改本地状态 → 界面当场更新，不用等 30 秒刷新 */
function patchLocalDone(url, done){
  const patch = (t) => {
    if(t.url !== url) return;
    t.manual = done;
    t.completed = done ? true : boolOf(t.submitted, t.graded);
  };
  (DATA?.tasks||[]).forEach(patch);
  (DATA?.courses||[]).forEach(c => (c.tasks||[]).forEach(patch));
}
const boolOf = (a,b) => !!(a || b);

function markDone(url, btn){
  const t = findTaskByUrl(url);
  if(!t){ toast('找不到该任务，请先刷新', '', ''); return; }
  if(btn){ btn.innerHTML = icon('i-refresh','sm'); btn.style.pointerEvents = 'none'; }

  apiMarkDone(url, t.title, t.course).then(res=>{
    if(res && res.ok === false){
      toast('标记失败：' + (res.error || '未知原因'), '', '');
      if(btn){ btn.innerHTML = icon('i-check','sm')+'我已完成';
               btn.style.pointerEvents = ''; }
      return;
    }
    patchLocalDone(url, true);
    toast('已标记完成，不再提示', '', '好');
    lastSig = '';                     /* 强制重绘 */
    render();
  }).catch(e=>{
    toast('标记失败：' + e, '', '');
    if(btn){ btn.innerHTML = icon('i-check','sm')+'我已完成';
             btn.style.pointerEvents = ''; }
  });
}

function unmarkDone(url, btn){
  const t = findTaskByUrl(url);
  if(btn){ btn.innerHTML = icon('i-refresh','sm'); btn.style.pointerEvents = 'none'; }

  apiUnmarkDone(url, t?.title || '', t?.course || '').then(res=>{
    if(res && res.ok === false){
      toast('撤销失败：' + (res.error || '未知原因'), '', '');
      return;
    }
    patchLocalDone(url, false);
    toast('已撤销标记', '', '好');
    lastSig = '';
    render();
  }).catch(e=>{
    toast('撤销失败：' + e, '', '');
  });
}

/* ══════════ 资料（实时浏览 ManageBac Files） ══════════
   用户要求：**实时**浏览和管理下载，不做本地镜像。
     * 文件夹点进去 → 实时向 ManageBac 请求该目录
     * 文件点一下 → 立刻开始下载到桌面
     * 面包屑可点，随时跳回上层
   每次进入栏目 / 切换课程 / 点文件夹都会重新拉取最新内容。 */

let FSTATE = {
  cid: '',            /* 当前课程 id */
  fid: '',            /* 当前文件夹 id，'' 表示根 */
  fname: '',          /* 当前文件夹名（从点击处记下，用于面包屑兜底） */
  data: null,         /* 最近一次拉取结果 */
  loading: false,
  err: '',
};

function apiListFiles(cid, fid, force){
  if(window.pywebview?.api?.files_list)
    return window.pywebview.api.files_list(cid, fid || '', !!force);
  if(isWebMode())
    return fetch('/api/files_list?cid=' + encodeURIComponent(cid)
      + '&fid=' + encodeURIComponent(fid || '')
      + (force ? '&force=1' : '')).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持',
                          folders:[], files:[], items:[]});
}

function apiFilesDownload(item){
  watchDownload();
  if(window.pywebview?.api?.files_download)
    return window.pywebview.api.files_download(item);
  if(isWebMode())
    return fetch('/api/files_download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(item)
    }).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

function apiFilesDownloadAll(cid, fid, className, folderName){
  watchDownload();
  if(window.pywebview?.api?.files_download_all)
    return window.pywebview.api.files_download_all(cid, fid || '',
                                                   className || '',
                                                   folderName || '');
  if(isWebMode())
    return fetch('/api/files_download_all?cid=' + encodeURIComponent(cid)
      + '&fid=' + encodeURIComponent(fid || '')
      + '&cls=' + encodeURIComponent(className || '')
      + '&folder=' + encodeURIComponent(folderName || '')).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

/* ---- 课程选择（下拉） ---- */
function fpicker(){
  const courses = DATA?.courses || [];
  const cur = FSTATE.cid || (courses[0]?.class_id || '');
  const opts = courses.map(c=>
    `<option value="${esc(c.class_id)}" ${c.class_id===cur?'selected':''}>
      ${esc(c.name)}</option>`).join('');
  return `<select class="fsel" id="fsel">${opts}</select>`;
}

/* ---- 面包屑 ----
   注意：后端 breadcrumb 包含「根目录 + 各级文件夹链接」。
   但最深的当前目录**有时不在链接里**（它是当前页，不是链接），
   所以这里用 FSTATE.fname 兜底补上，保证用户总能看到自己在哪。 */
function fcrumbs(d){
  const cur = FSTATE.cid || '';
  const cl = (DATA?.courses||[]).find(x=>x.class_id===cur);
  const bc = (d?.breadcrumb) || [];

  let h = `<i data-fgo="" title="回到根目录${cl?.name?'（'+esc(cl.name)+'）':''}"`
        + `>${esc(cl?.name || '资料')}</i>`;

  /* 子层级（跳过第 0 项，它就是课程本身） */
  for(let i=1; i<bc.length; i++){
    const fid = bc[i].folderId || '';
    const isCur = (fid === FSTATE.fid);
    h += `<span class="fsep">/</span>`;
    h += isCur
      ? `<i class="fcur" title="当前目录">${esc(bc[i].name)}</i>`
      : `<i data-fgo="${esc(fid)}">${esc(bc[i].name)}</i>`;
  }

  /* 兜底：面包屑里没有当前目录（后端没给），就用点击时记下的名字补上 */
  const lastFid = bc.length ? (bc[bc.length-1].folderId || '') : '';
  if(FSTATE.fid && FSTATE.fname && lastFid !== FSTATE.fid){
    h += `<span class="fsep">/</span>`
       + `<i class="fcur" title="当前目录">${esc(FSTATE.fname)}</i>`;
  }
  return h;
}

/* ---- 上级目录 id（给「返回」按钮用） ----
   优先级：面包屑里上一级 > 根目录('')；已在根目录则返回 null（不显示按钮） */
function fParentId(){
  if(!FSTATE.fid) return null;          /* 已在根目录 → 无上级 */
  const bc = (FSTATE.data?.breadcrumb) || [];
  for(let i=bc.length-1; i>=0; i--){
    if((bc[i].folderId || '') === FSTATE.fid){
      /* 找到当前目录 → 取它前面一个 */
      return i >= 1 ? (bc[i-1].folderId || '') : '';
    }
  }
  /* 面包屑里没有当前目录 → 上一级 = 面包屑最后一项 */
  if(bc.length >= 2) return bc[bc.length-1].folderId || '';
  return '';                            /* 兜底回根目录 */
}

/* ---- 单个文件行 ---- */
function frow(it, idx){
  const isDir = it.kind === 'folder';
  const col = isDir ? 'var(--yellow)' : fileColor(it.name);
  const ic = icon(isDir ? 'i-folder' : fileIconFor(it.name), 'lg');
  const wait = (idx||0);
  const sub = [];
  if(it.size) sub.push(`<span>${esc(it.size)}</span>`);
  if(it.author) sub.push(`<span>by ${esc(it.author)}</span>`);
  if(it.modified) sub.push(`<span>${esc(it.modified)}</span>`);

  return `<div class="frow${isDir?' isdir':''}" `
    + `style="--c:${col};animation-delay:${Math.min(wait*18, 260)}ms" `
    + `data-fitem='${esc(JSON.stringify(it))}'>
    <span class="fic">${ic}</span>
    <span class="fmeta">
      <span class="fname">${esc(it.name)}</span>
      ${sub.length?`<span class="fsub">${sub.join('')}</span>`:''}
    </span>
    ${isDir ? `<span class="fgo">${icon('i-chevright','sm')}</span>`
            : `<span class="fact">${icon('i-download','sm')}<em>下载</em></span>`}
  </div>`;
}

function fileColor(name){
  const e = (name||'').split('.').pop().toLowerCase();
  if(e==='pdf') return 'var(--red)';
  if(['doc','docx'].includes(e)) return 'var(--blue)';
  if(['xls','xlsx','csv'].includes(e)) return 'var(--green)';
  if(['ppt','pptx'].includes(e)) return 'var(--orange)';
  if(['png','jpg','jpeg','gif','webp','svg'].includes(e)) return 'var(--pink)';
  if(['zip','rar','7z'].includes(e)) return 'var(--violet)';
  if(['mp4','mov','avi','mkv'].includes(e)) return 'var(--teal)';
  if(['mp3','wav','m4a'].includes(e)) return 'var(--cyan)';
  return 'var(--muted)';
}

/* ---- 整个「资料」页 ---- */
function filesPage(){
  const cl0 = (DATA?.courses||[]).find(x=>x.class_id===FSTATE.cid);

  /* 骨架屏（首次进入时比转圈更有「质感」） */
  if(FSTATE.loading && !FSTATE.data){
    let sk = '';
    for(let i=0;i<5;i++) sk += `<div class="fskel" style="animation-delay:${i*70}ms"></div>`;
    return `<div class="fwrap">
      <div class="fbar">
        ${fpicker()}
        <span class="flive"><span class="dot"></span>实时</span>
      </div>
      <div class="fskels">${sk}</div>
    </div>`;
  }

  const d = FSTATE.data;
  const cl = cl0;
  const pid = fParentId();
  const canBack = pid !== null;

  let h = `<div class="fwrap">`;

  /* 顶部：课程选择 + 刷新 */
  h += `<div class="fbar">
    ${fpicker()}
    <span class="flive"><span class="dot"></span>实时</span>
    <button class="fb" id="frefresh" title="刷新当前目录">${icon('i-refresh')}</button>
  </div>`;

  /* 面包屑 + 返回上一级 + 在浏览器打开 */
  if(d && d.ok){
    h += `<div class="fbar fnav">
      ${canBack
        ? `<button class="fb fback" id="fback" title="返回上一级">${icon('i-back')}</button>`
        : ''}
      <span class="fpath">${fcrumbs(d)}</span>
      <button class="fb" id="fopenweb"
        title="在浏览器打开这个目录">${icon('i-external')}</button>
    </div>`;
  }

  if(FSTATE.err){
    h += `<div class="fempty">
      <div class="fi">${icon('i-alert')}</div>
      <div class="ft">读取失败</div>
      <div class="fd">${esc(FSTATE.err)}</div>
      <button class="fretry" id="fretry">${icon('i-refresh','sm')}<em>重试</em></button>
    </div></div>`;
    return h;
  }

  if(!d || !d.ok){
    h += `<div class="fempty">
      <div class="fi">${icon('i-folder')}</div>
      <div class="ft">暂无资料</div>
      <div class="fd">这门课的 Files 里还没有文件。</div>
    </div></div>`;
    return h;
  }

  const items = d.items || [];
  if(!items.length){
    h += `<div class="fempty">
      <div class="fi">${icon('i-inbox')}</div>
      <div class="ft">这个文件夹是空的</div>
      <div class="fd">${esc(cl?.name || '')}</div>
      ${canBack
        ? `<button class="fretry" id="fback2">${icon('i-back','sm')}<em>返回上一级</em></button>`
        : ''}
    </div></div>`;
    return h;
  }

  /* 全部下载（当前目录的文件） */
  if((d.files||[]).length){
    h += `<button class="fdlall" id="fdlall">
      ${icon('i-download','sm')}<em>下载本目录全部文件（${d.files.length} 个）</em></button>`;
  }

  h += items.map((it,i)=>frow(it,i)).join('');
  h += `<div style="height:12px"></div></div>`;
  return h;
}

/* ---- 拉取（实时） ---- */
function filesLoad(fid, force, fname){
  FSTATE.fid = fid || '';
  /* 记下文件夹名 —— 后端面包屑有时不含当前目录，靠它兜底显示 */
  if(fname !== undefined) FSTATE.fname = fname || '';
  else if(!FSTATE.fid) FSTATE.fname = '';
  FSTATE.loading = true;
  FSTATE.err = '';
  const box = $('#page-files');
  if(box) box.innerHTML = filesPage();
  bindFiles();

  apiListFiles(FSTATE.cid, FSTATE.fid, force).then(d=>{
    FSTATE.loading = false;
    if(!d || d.ok === false){
      FSTATE.err = (d && d.error) || '未登录或网络异常';
      FSTATE.data = null;
    }else{
      FSTATE.data = d;
    }
    const bx = $('#page-files');
    if(bx) bx.innerHTML = filesPage();
    bindFiles();
    filesBadge();
  }).catch(e=>{
    FSTATE.loading = false;
    FSTATE.err = String(e);
    const bx = $('#page-files');
    if(bx) bx.innerHTML = filesPage();
    bindFiles();
  });
}

function filesBadge(){
  const bb = $('#badge-files');
  if(!bb) return;
  const d = FSTATE.data;
  if(d && d.ok){
    const n = (d.folders||[]).length + (d.files||[]).length;
    bb.textContent = n || '0';
    bb.title = `${(d.folders||[]).length} 个文件夹 / ${(d.files||[]).length} 个文件`;
  }else{
    bb.textContent = '—';
  }
}

/* ---- 事件绑定 ---- */
function bindFiles(){
  const box = $('#page-files');
  if(!box) return;

  /* 课程切换 */
  const sel = $('#fsel');
  if(sel) sel.addEventListener('change', ()=>{
    FSTATE.cid = sel.value;
    FSTATE.data = null;
    FSTATE.fname = '';
    filesLoad('', true, '');
  });

  /* 刷新 */
  const rf = $('#frefresh');
  if(rf) rf.addEventListener('click', ()=>{
    rf.classList.add('spin');
    filesLoad(FSTATE.fid, true);
  });

  /*  返回上一级 */
  const bk = $('#fback');
  if(bk) bk.addEventListener('click', ()=>{
    const pid = fParentId();
    if(pid === null) return;
    filesLoad(pid, false);
  });
  const bk2 = $('#fback2');
  if(bk2) bk2.addEventListener('click', ()=>{
    const pid = fParentId();
    filesLoad(pid === null ? '' : pid, false);
  });

  /* 读取失败 → 重试 */
  const rt = $('#fretry');
  if(rt) rt.addEventListener('click', ()=> filesLoad(FSTATE.fid, true));

  /* 浏览器打开当前目录 */
  const ow = $('#fopenweb');
  if(ow) ow.addEventListener('click', ()=>{
    const c = (DATA?.courses||[]).find(x=>x.class_id===FSTATE.cid);
    if(!c) return;
    openURL(FSTATE.fid
      ? `${c.url}/files/folder/${FSTATE.fid}`
      : `${c.url}/files`);
  });

  /* 面包屑跳转 */
  box.querySelectorAll('[data-fgo]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      const fid = el.dataset.fgo || '';
      /* 目标层级名（点的时候就知道，用于面包屑兜底） */
      filesLoad(fid, false, fid ? (el.textContent || '').trim() : '');
    });
  });

  /* 目录内全部下载 */
  const da = $('#fdlall');
  if(da) da.addEventListener('click', ()=>{
    if(da.dataset.busy) return;
    da.dataset.busy = '1';
    const old = da.innerHTML;
    da.innerHTML = icon('i-refresh','sm') + '<em>正在下载……</em>';
    da.classList.add('spin');
    const c = (DATA?.courses||[]).find(x=>x.class_id===FSTATE.cid);
    const d = FSTATE.data;
    const crumb = (d?.breadcrumb||[]).slice(1).map(x=>x.name).join(' / ');
    Promise.resolve(apiFilesDownloadAll(FSTATE.cid, FSTATE.fid, c?.name || '', crumb
    )).finally(()=>{
      delete da.dataset.busy;
      da.innerHTML = old;
      da.classList.remove('spin');
    });
  });

  /* 行列点击：文件夹进入 / 文件下载 */
  box.querySelectorAll('[data-fitem]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      let it;
      try{ it = JSON.parse(el.dataset.fitem); }catch(_){ return; }

      if(it.kind === 'folder'){
        filesLoad(it.folderId, false, it.name);
        return;
      }
      /* 文件 → 立刻下载 */
      if(el.classList.contains('dling')) return;
      el.classList.add('dling');
      const bar = document.createElement('div');
      bar.className = 'fprog';
      el.appendChild(bar);
      const fact = el.querySelector('.fact');
      const factOld = fact ? fact.innerHTML : '';
      if(fact) fact.innerHTML = icon('i-refresh','sm');
      toast('开始下载 ' + it.name, '', '');

      /* 附带课程名与文件夹路径，后端据此决定保存位置 */
      const c = (DATA?.courses||[]).find(x=>x.class_id===FSTATE.cid);
      const crumb = (FSTATE.data?.breadcrumb||[]).slice(1)
                      .map(x=>x.name).join('/');
      const payload = {...it, className: c?.name || '', subDir: crumb};

      Promise.resolve(apiFilesDownload(payload)).then(res=>{
        /* 实际完成由 Python 端回调 downloadDone 通知；
           这里只负责把「下载中」的视觉状态恢复 */
        if(res && res.ok === false){
          el.classList.remove('dling');
          bar.remove();
          if(fact) fact.innerHTML = factOld;
          toast('下载失败：' + (res.error || '未知原因'), 'err', '');
          return;
        }
        /* 乐观：1.2 秒后恢复正常（大文件仍会继续下载） */
        setTimeout(()=>{
          el.classList.remove('dling');
          bar.remove();
          if(fact) fact.innerHTML = factOld;
        }, 1200);
      }).catch(err=>{
        el.classList.remove('dling');
        bar.remove();
        if(fact) fact.innerHTML = factOld;
        toast('下载失败：' + err, 'err', '');
      });
    });
  });
}

/* ══════════ 任务详情面板 ══════════
   用户要求：点击作业展开菜单，包含
       附件（如果有） / 老师要求 / GPA（若已登记）
       外加一个按键跳转原链接。

   设计：
     * 面板插在卡片**下方**（不是弹窗）—— 滚动位置不跳动
     * 详情按任务缓存 24 小时 → 第二次点开是几乎立即的
     * 附件可直接下载 */

let DPANEL_URL = null;        /* 当前展开的面板对应的任务 */

/* ══════════ 详情的新鲜度（0.5.2 修）══════════
 *
 * 【原来为什么坏】
 *   apiTaskDetail 固定传 force=false，而后端 taskdetail.fetch 的
 *   语义是「命中缓存就直接返回，缓存 24 小时」：
 *
 *       app/taskdetail.py
 *         if not force:
 *             c = _read_cache(tid)      # 24 小时内直接返回
 *             if c: return c            # ← 含旧成绩
 *
 *   于是老师改了分之后：
 *     · 列表页会更新（每 5 分钟重建，读的是 DATA）
 *     · **点开详情看到的还是旧成绩**，而且一整天都不会变
 *   用户看到的就是「一直保持原成绩」。
 *
 * 【现在怎么做】
 *   1. 用**列表页已经抓到的成绩**覆盖详情里的成绩。
 *      列表每 5 分钟刷新一次，它才是当下最新的事实。
 *      这一步零成本（数据就在内存里），且立刻见效。
 *   2. 详情本身加一个「最短 revisit 间隔」：超过 3 分钟再点开就真去抓一次。
 *      不再让 24 小时缓存挡住成绩变化。
 *      3 分钟是折中 —— 连着点开几个任务不会被站点限速，
 *      而改分这种低频事件最多 3 分钟就能看到。
 */
const DPANEL_REVISIT_MS = 3 * 60 * 1000;

/* 已展开过的任务：tid → 上次真正联网抓取的时间 */
const DPANEL_FETCHED = new Map();

function dpTidOf(url){
  const m = /\/core_tasks\/(\d+)/.exec(url || '');
  return m ? m[1] : (url || '');
}

/* 该不该真去抓一次 */
function dpNeedsFetch(url){
  const tid = dpTidOf(url);
  if(!tid) return true;
  const last = DPANEL_FETCHED.get(tid);
  return !last || (Date.now() - last) >= DPANEL_REVISIT_MS;
}

function dpMarkFetched(url){
  const tid = dpTidOf(url);
  if(tid) DPANEL_FETCHED.set(tid, Date.now());
}

function apiTaskDetail(url, force){
  /*
      ★ force=true 时后端会**跳过缓存**直接抓页面。
        注意别在同一个任务上反复用 true —— 站点有限速。
  */
  const f = !!force;
  if(f) dpMarkFetched(url);
  if(window.pywebview?.api?.task_detail)
    return window.pywebview.api.task_detail(url, f);
  if(isWebMode())
    return fetch('/api/task_detail?url=' + encodeURIComponent(url)
                 + (f ? '&force=1' : ''))
      .then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持', attachments:[],
                          description:'', grade:{}});
}

/* 用列表页的成绩覆盖详情里的成绩。
 *
 * 【为什么只覆盖成绩，不动别处】
 *   描述 / 附件在老师改分时不会变，用缓存没问题（还省流量）；
 *   只有成绩是「会被人改」的字段，所以要以后者为准。
 *
 * 【为什么要判空】
 *   列表页有时读不到成绩（页面没渲染完 / 字段名变了）。
 *   那种情况下**不要**拿空值去覆盖详情里已有的成绩 ——
 *   宁可显示略旧的正确值，也不要显示一个空白。
 */
function dpPreferListGrade(d, url){
  try{
    const t = findTaskByUrl(url);
    if(!t) return d;
    const g = (d && d.grade) || {};

    /* 列表页说「有成绩」才覆盖 */
    const hasListGrade = t.graded && t.score != null && t.score !== ''
                         && t.score !== 'N/A';
    if(!hasListGrade) return d;

    const listScore = numOf(t.score);
    if(listScore == null) return d;

    /* 已经是同一个值就别动（避免无谓的重绘） */
    if(g.score === listScore && g.percent != null) return d;

    /*  ★ 成绩以**列表页**为准：它是每 5 分钟刷新的新鲜数据，
        详情缓存里的那个可能是几小时前的（老师改分前）。
        这里必须让列表页赢 —— 写反了就等于没修。 */
    const merged = Object.assign({}, g, {
      score: listScore,
      percent: (t.percent != null) ? t.percent
               : ((g.percent != null) ? g.percent
                  : (t.percent == null && listScore != null
                     ? listScore : null)),
      level: t.level || g.level || '',
      text: (t.level ? t.level + ' ' : '') + listScore
            + (g.max != null ? ' / ' + g.max : ''),
    });
    return Object.assign({}, d, { grade: merged, grade_from_list: true });
  }catch(e){
    return d;
  }
}

/* 在 DATA 里按 URL 反查任务（实现在前面，见 findTaskByUrl） */

/* 宽松地把 "88" / "88%" / "88 / 100" 变成数字 */
function numOf(v){
  if(typeof v === 'number') return v;
  const m = /(\d+(?:\.\d+)?)/.exec(String(v == null ? '' : v));
  return m ? parseFloat(m[1]) : null;
}

function fileIconFor(name){
  const e = (name || '').split('.').pop().toLowerCase();
  if(e === 'pdf') return 'i-file';
  if(['doc','docx','rtf','odt'].includes(e)) return 'i-doc';
  if(['xls','xlsx','csv','ods'].includes(e)) return 'i-table';
  if(['ppt','pptx','odp','key'].includes(e)) return 'i-slides';
  if(['png','jpg','jpeg','gif','webp','svg','bmp','ico'].includes(e)) return 'i-image';
  if(['zip','rar','7z','tar','gz'].includes(e)) return 'i-zip';
  if(['mp4','mov','avi','mkv','webm'].includes(e)) return 'i-video';
  if(['mp3','wav','m4a','flac','ogg'].includes(e)) return 'i-audio';
  return 'i-file';
}

/* 统一的图标 HTML 生成器 */
function icon(name, cls){
  return `<svg class="oi ${cls||''}"><use href="#${name}"/></svg>`;
}
/* 把按钮内容换成「加载中」的转圈图标（保留布局尺寸） */
/* 打开时的「骨架」—— 立刻显示我们**已经知道**的信息
   （标题 / 课程 / 截止 / 成绩 / 类型），只把「老师要求 + 附件」
   留成占位。这样用户点下去**立刻有反馈**，不用干等 1 秒。

   数据来源：列表页已经抓到的 task 对象（前端内存里就有，零延迟）。 */
function dpSkeleton(t){
  t = t || {};
  const tags = [];
  if(t.course) tags.push(t.course);
  if(t.kind) tags.push(t.kind);
  if(t.category) tags.push(t.category);
  if(t.due_date) tags.push('截止 ' + t.due_date);
  const hasScore = t.graded && t.score && t.score !== 'N/A';

  return `<div class="dpanel" data-dp="${esc(t.url || '')}">
    <div class="dp-head">
      <span class="t">${esc(t.title || '任务详情')}</span>
      <button class="dp-x" data-dpclose="1" title="收起">${icon('i-x','sm')}</button>
    </div>
    <div class="dp-body">
      ${tags.length?`<div class="dp-tags">${tags.map(x=>
        `<span class="tag">${esc(x)}</span>`).join('')}</div>`:''}

      ${hasScore?`<div class="dp-block"><div class="dp-label">
        ${icon('i-award','sm inl')}成绩</div>
        <div class="dp-grade">
          ${t.level?`<span class="gv" style="color:${lvlColor(t.level)}"
            >${esc(t.level)}</span>`:''}
          <span class="gv" style="color:${t.percent!=null?pctColor(t.percent):'var(--text)'}"
          >${esc(t.score)}</span>
          ${t.percent!=null?`<span class="gp">${t.percent}%</span>`:''}
        </div></div>`:''}

      <!-- 下面这两块需要联网抓，先显示占位 -->
      <div class="dp-block"><div class="dp-label">
        ${icon('i-clip','sm inl')}附件</div>
        <div class="dp-wait">
          <span class="dp-skel w60"></span>
        </div></div>

      <div class="dp-block"><div class="dp-label">
        ${icon('i-note','sm inl')}老师要求</div>
        <div class="dp-wait">
          <span class="dp-skel"></span>
          <span class="dp-skel w80"></span>
          <span class="dp-skel w45"></span>
        </div></div>

      <div class="dp-wait-note">
        <span class="spin-ic">${icon('i-refresh','sm')}</span>正在读取附件与老师要求……
      </div>
    </div>
  </div>`;
}

/* 兼容旧调用（不需要任务信息时） */
function dpRender(url, d){
  const atts = d.attachments || [];
  const desc = (d.description || '').trim();
  const g = d.grade || {};
  const gtxt = g.text || '';

  let h = `<div class="dpanel" data-dp="${esc(url)}">
    <div class="dp-head">
      <span class="t">${esc(d.title || '任务详情')}</span>
      <button class="dp-x" data-dpclose="1" title="收起">${icon('i-x','sm')}</button>
    </div>
    <div class="dp-body">`;

  if(!d.ok && d.error){
    h += `<div class="dp-err">${icon('i-alert','sm inl')}${esc(d.error)}</div>`;
  }

  /* ── 分类标签 ── */
  const tags = [];
  if(d.category) tags.push(d.category);
  if(d.kind) tags.push(d.kind);
  if(d.due_text) tags.push('截止 ' + d.due_text);
  if(d.status) tags.push(d.status);
  if(tags.length){
    h += `<div class="dp-tags">${tags.map(t=>
      `<span class="tag">${esc(t)}</span>`).join('')}</div>`;
  }

  /* ── 成绩（若已被登记） ── */
  h += `<div class="dp-block"><div class="dp-label">
    ${icon('i-award','sm inl')}成绩</div>`;
  const hasGrade = gtxt && !/not submitted|n\/a|待评/i.test(gtxt);
  if(hasGrade){
    const lv = g.level || '';
    const sc = (g.score != null && g.max != null)
      ? `${g.score} / ${g.max}` : '';
    const pc = g.percent != null ? `${g.percent}%` : '';
    h += `<div class="dp-grade">
      ${lv?`<span class="gv" style="color:${lvlColor(lv)}">${esc(lv)}</span>`:''}
      <span class="gv" style="color:${g.percent!=null?pctColor(g.percent):'var(--text)'}">
        ${esc(sc || gtxt)}</span>
      ${pc?`<span class="gp">${esc(pc)}</span>`:''}
    </div>`;
  }else{
    h += `<div class="dp-grade na"><span class="gv">
      ${esc(gtxt || '尚未登记成绩')}</span></div>`;
  }
  h += `</div>`;

  /* ── 教师评语 ──
     ★ 只有真的有评语才出这张卡。
       没有就**整个块都不渲染** —— 空卡片比没有卡片更让人困惑：
       用户会以为是加载失败。 */
  const cmt = (d.comment || '').trim();
  if(cmt){
    const who = (d.comment_author || '').replace(/\s+/g, ' ').trim();
    h += `<div class="dp-block dp-comment">
      <div class="dp-label">${icon('i-note','sm inl')}教师评语</div>
      <div class="dp-cmt">${esc(cmt)}</div>
      ${who ? `<div class="dp-cmtby">${esc(who)}</div>` : ''}
    </div>`;
  }

  /* ── 附件 ── */
  if(atts.length){
    h += `<div class="dp-block"><div class="dp-label">
      ${icon('i-clip','sm inl')}附件（${atts.length}）</div>`;
    h += atts.map(a=>`<div class="dp-file"
        data-dpfile="${esc(a.url)}" data-dpname="${esc(a.name)}"
        title="点击下载 ${esc(a.name)}">
      <span class="fic">${icon(fileIconFor(a.name),'sm')}</span>
      <span class="fname">${esc(a.name)}</span>
      ${a.size?`<span class="fsz">${esc(a.size)}</span>`:''}
      <span class="fdl">${icon('i-download','sm')}</span>
    </div>`).join('');
    h += `</div>`;
  }

  /* ── 老师要求 ── */
  h += `<div class="dp-block"><div class="dp-label">
    ${icon('i-note','sm inl')}老师要求</div>`;
  if(desc){
    h += `<div class="dp-desc">${esc(desc)}</div>`;
  }else{
    h += `<div class="dp-desc" style="color:var(--dim)">
      老师没有写补充说明。</div>`;
  }
  h += `</div>`;

  /* ── 跳转原链接 ── */
  h += `<button class="dp-open" data-dpopen="${esc(url)}">
    ${icon('i-external','sm')}打开 ManageBac 原网页</button>`;

  h += `</div></div>`;
  return h;
}

/*  面板的外部包层：真正的动画载体
   dp-outer（grid 高度动画 + 透明度）
     └ dp-clip（overflow:hidden，高度动画必需）
         └ .dpanel（缩放/位移/色条，真正的卡片） */
function dpWrap(inner, url){
  return `<div class="dp-outer" data-dpwrap="${esc(url)}">
    <div class="dp-clip">${inner}</div></div>`;
}

/* 把新内容替换掉加载态，并让内容「淡入」而不是硬切 */
function dpSwap(url, panel0, html){
  const panel = document.querySelector(`.dpanel[data-dp="${CSS.escape(url)}"]`) || panel0;
  if(!panel || !panel.parentNode) return;
  const fresh = document.createElement('div');
  fresh.innerHTML = html;
  const next = fresh.firstElementChild;
  next.classList.add('dp-fresh');          /*  淡入 */
  panel.replaceWith(next);
  bindDetail();

  const wrap = next.closest('.dp-outer');
  if(!wrap) return;
  /*  打上标记：stagger 已经在首次展开时播过了，
     内容替换不再重播（否则会闪两下）。 */
  wrap.classList.add('dp-swapped');

  /*  重新量高。两种情况：
       ① 展开动画还在跑 → 平滑过渡到新的高度
       ② 已经解除限高（max-height:none）→ 不用管，让它自由伸展 */
  if(wrap.classList.contains('open') && wrap.style.maxHeight !== 'none'){
    wrap.style.maxHeight = dpMeasure(wrap) + 'px';
  }
}

/*  量出面板的**真实高度**（用来做精确的 max-height 动画）
   —— 不猜数字，所以动画曲线是线性的，不会出现
   「前 90% 时间看不出变化、最后突然展开完」那种模糊感。 */
function dpMeasure(wrap){
  const panel = wrap.querySelector('.dpanel');
  if(!panel) return 0;
  /* 用 offsetHeight（含 border）+ 上下 margin，确保新内容不被裁 */
  const cs = getComputedStyle(panel);
  const mt = parseFloat(cs.marginTop) || 0;
  const mb = parseFloat(cs.marginBottom) || 0;
  return panel.offsetHeight + mt + mb;
}

/*  展开：先坐实初始态，再设精确高度 */
function dpOpen(wrap){
  if(!wrap) return;
  /* 强制重排 —— 浏览器会把同帧内的样式修改合并，
     不先读一次布局，「0 高度」就坐实不了，transition 不触发。
      刻意不用 requestAnimationFrame：页面在后台被节流时
       rAF 可能一直不触发，面板就永远停在 0 高度。 */
  void wrap.offsetHeight;
  wrap.classList.add('open');
  wrap.style.maxHeight = dpMeasure(wrap) + 'px';

  /* 展开动画结束后解除限高 ——
     之后图片加载、文字换行都不会被裁掉。 */
  const settle = ()=>{
    if(!wrap.classList.contains('open') || wrap.dataset.closing) return;
    wrap.style.maxHeight = 'none';
  };
  wrap.addEventListener('transitionend', function onEnd(e){
    if(e.propertyName !== 'max-height') return;
    wrap.removeEventListener('transitionend', onEnd);
    settle();
  });
  setTimeout(settle, 700);        /* 双保险（万一 transitionend 没来） */
}

function toggleDetail(url, cardEl){
  /* 再点一次同一个 → 收起 */
  const existing = cardEl.nextElementSibling;
  if(existing && existing.classList.contains('dp-outer')
     && !existing.dataset.closing){
    dpClose(existing);
    return;
  }
  /* 其他面板先关掉（同时只开一个，界面清爽）—— 同样带动画 */
  document.querySelectorAll('.dp-outer').forEach(el=>dpClose(el, true));

  /*  立刻显示已知信息（标题/课程/截止/成绩），只有附件和
     老师要求留占位 —— 用户点下去马上有反馈，不用干等 */
  const t = findTaskByUrl(url) || {};
  const box = document.createElement('div');
  box.innerHTML = dpWrap(dpSkeleton({...t, url}), url);
  const wrap0 = box.firstElementChild;
  const panel0 = wrap0.querySelector('.dpanel');
  cardEl.after(wrap0);
  DPANEL_URL = url;
  bindDetail();                 /* 让「收起」按钮立刻可用 */

  dpOpen(wrap0);

  /*  源卡片回弹：短暂高亮，说明面板是从它里面出来的 */
  if(cardEl && cardEl.classList){
    cardEl.classList.add('dp-src');
    setTimeout(()=>cardEl.classList.remove('dp-src'), 520);
  }

  /*  ★ 该不该真去抓一次 —— 由 dpNeedsFetch 按「距上次抓取多久」决定。
      以前这里固定 force=false，结果 24 小时的缓存一直挡在前面，
      老师改过分也看不到。现在超过 3 分钟就重新抓。
      （见文件上方「详情的新鲜度」注释） */
  const needFresh = dpNeedsFetch(url);

  apiTaskDetail(url, needFresh).then(raw=>{
    /*  成绩以**列表页**为准 —— 它每 5 分钟刷新一次，
        是当下最新的事实；详情缓存里的成绩可能已经过时。
        描述 / 附件仍用详情的（那些内容不会被人改）。 */
    const d = dpPreferListGrade(raw, url);
    dpSwap(url, panel0, dpRender(url, d));
  }).catch(e=>{
    dpSwap(url, panel0, dpRender(url,
      {ok:false, error:String(e), attachments:[], description:'', grade:{}}));
  });
}

/*  关闭：先把「限高」从 none 拽回具体值，再收到 0。
   —— 直接给 max-height:none → 0 是不通的，中间必须有具体值做起点。
   `now=true` 表示这是「开另一个面板时顺手关掉旧的」，
   这种要快一点，不然新面板要等旧的演完。 */
function dpClose(wrap, now){
  if(!wrap || wrap.dataset.closing) return;
  wrap.dataset.closing = '1';

  /* 起点：从当前真实高度开始收（不能从 none 开始） */
  wrap.style.maxHeight = dpMeasure(wrap) + 'px';
  void wrap.offsetHeight;                  /* 坐实起点 */

  wrap.classList.remove('open');
  wrap.classList.add('closing');
  wrap.style.maxHeight = '0px';

  setTimeout(()=>wrap.remove(), now ? 170 : 320);
}

function bindDetail(){
  /* 收起 ——  走 dpClose，带动画 */
  document.querySelectorAll('.dpanel [data-dpclose]').forEach(b=>{
    b.addEventListener('click', e=>{
      e.stopPropagation();
      dpClose(b.closest('.dp-outer'));
    });
  });
  /* 下载附件 */
  document.querySelectorAll('.dpanel [data-dpfile]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      const url = el.dataset.dpfile, name = el.dataset.dpname;
      if(el.dataset.busy) return;
      el.dataset.busy = '1';
      const badge = el.querySelector('.fdl');
      if(badge) badge.innerHTML = icon('i-refresh','sm');
      toast('正在下载 ' + name + ' ……', '', '');
      Promise.resolve(apiDownloadFile(url, name, '')).finally(()=>{
        delete el.dataset.busy;
        if(badge) badge.innerHTML = icon('i-download','sm');
      });
    });
  });
  /* 跳转原链接 */
  document.querySelectorAll('.dpanel [data-dpopen]').forEach(b=>{
    b.addEventListener('click', e=>{
      e.stopPropagation();
      openURL(b.dataset.dpopen);
    });
  });
}

/* ══════════════════════════════════════════════════════════════════
   首次运行引导页

    这是「普通用户双击就能用」的关键：
     没有它，用户双击后看到的是一个空界面 + 一句技术错误，
     然后不知道该怎么办。

    交互设计（都是为了让不懂技术的人也能填对）：
     ① 学校网址默认填好、可改 —— 大多数人是同一个学校
     ② 密码默认隐藏，一个眼睛按钮切换
     ③ **填完先测登录**，失败当场告诉他原因，而不是静默失败
     ④ 测试失败时**不保存**，避免把错的凭据留在磁盘上
     ⑤ 成功后才保存，然后自动进入主界面并开始抓取
     ══════════════════════════════════════════════════════════════════ */

/* 从 URL 里取出域名部分（用户可能整段粘贴）
   "https://x.managebac.cn/student/home" → "x.managebac.cn" */
function suHostOf(u){
  u = (u || '').trim();
  u = u.replace(/^https?:\/\//i, '');
  return u.split('/')[0].replace(/\/+$/, '');
}

function suMsg(kind, html){
  const el = $('#suMsg');
  if(!el) return;
  el.className = 'su-msg show ' + kind;
  el.innerHTML = html;
}
function suClearMsg(){
  const el = $('#suMsg');
  if(el) el.className = 'su-msg';
}

/* 构建引导页（图标 + 眼睛 + 按钮文案都用 icon()，保持一致）
   ★ 三个页面各有自己的按钮，一次都建好 */
function buildSetup(){
  const ic = $('#suIc'); if(ic) ic.innerHTML = icon('i-shield','lg');
  $('#suEye')?.replaceChildren();
  const e1 = $('#suEye'); if(e1) e1.innerHTML = icon('i-eye','sm');
  const e2 = $('#suSchEye'); if(e2) e2.innerHTML = icon('i-eye','sm');
  const g1 = $('#suGo');
  if(g1) g1.innerHTML = icon('i-check','sm') + '<span>登录并继续</span>';
  const g2 = $('#suGo2');
  if(g2) g2.innerHTML = icon('i-check','sm') + '<span>保存并继续</span>';
  const g3 = $('#suGo3');
  if(g3) g3.innerHTML = icon('i-check','sm') + '<span>完成</span>';
  suPageSet(1, {force: true});
}

/* ══════════ 分页引导（一个网站一页）══════════

   ★ 用户的反馈：
     「初次开启时，输入帐密，一个网站一个页面，方便分辨。」

   三页：
     1. ManageBac（学校系统）—— 必填
     2. 课表（希悦校园）    —— 可跳过
     3. EC 名单名字         —— 可跳过

   ★ 为什么用「页面」而不是一页堆三段：
     要填的是**两个不同网站**的账号，形态还不一样
     （一个是学校邮箱、一个是手机号）。
     堆在一页上填错了才报错，还得回头找是哪个框。
     分页之后每页顶上都写着「这是哪个系统」。

   ★ 切页用类名 + CSS 动画，不用重建 DOM ——
     重建会把用户已经填进去的内容丢掉。 */
let SU_PAGE = 1;
const SU_PAGES = 3;

/* 跳到第 n 页。back=true 时入场方向反过来（往回翻的手感）。 */
function suPageSet(n, opt){
  n = Math.max(1, Math.min(SU_PAGES, n | 0));
  const prev = SU_PAGE;
  const back = !!(opt && opt.back) || n < prev;
  const force = !!(opt && opt.force);

  for(let i = 1; i <= SU_PAGES; i++){
    const el = document.getElementById('suP' + i);
    if(!el) continue;
    el.classList.remove('on', 'out', 'back');
    if(i === n){
      el.classList.add('on');
      if(back) el.classList.add('back');
    }
  }
  /*  步骤点：已在的实心、当前的高亮、没到的浅色。
      这样能看出「共几页 / 走到哪了」。 */
  const dots = document.querySelectorAll('#suDots i');
  dots.forEach((d, idx)=>{
    d.classList.toggle('on', idx === n - 1);
    d.classList.toggle('done', idx < n - 1);
  });

  SU_PAGE = n;
  suUpdateHead();
  suClearMsg();
  /*  换页之后聚焦到这一页的第一个输入框 —— 键盘用户不用再点一下。
      带 force 的那次（建页面时）不聚焦，否则会抢走主界面的焦点。 */
  if(!force){
    setTimeout(()=>{
      try{
        const el = document.querySelector('#suP' + n + ' .su-in');
        el?.focus();
      }catch(e){}
    }, 180);
  }
}

/* 每页的标题 / 副标题 —— 说的都是「这一页在填谁」 */
function suUpdateHead(){
  const T = $('#suTitle'), S = $('#suSub'), F = $('#suFoot');
  if(T){
    T.textContent = ({1:'登录', 2:'课表', 3:'EC 名单'})[SU_PAGE] || '登录';
  }
  if(S){
    S.textContent = ({
      1:'学校系统，必填',
      2:'希悦校园，可以跳过',
      3:'可不填',
    })[SU_PAGE] || '';
  }
  if(F){
    F.textContent = SU_PAGE === 1
      ? '账号保存在本机。'
      : ('第 ' + SU_PAGE + ' / ' + SU_PAGES + ' 步');
  }
}

function openSetup(page){
  const el = $('#setup');
  if(!el) return;
  el.classList.add('open');
  document.body.classList.add('setting-up');
  /* 网址默认值：从后端拿到的默认站点里抽域名。
     ★ 占位域名不预填 —— 预填进去用户会以为已是对的，
       直接提交打到假域名上。留空让输入框自己的 placeholder
       露出来，反而更显眼。 */
  const u = $('#suUrl');
  if(u && !u.value){
    const h = suHostOf(DEFAULT_HOST || '');
    if(h && h !== SETUP_PLACEHOLDER_HOST) u.value = h;
  }
  /*  回填已经存过的东西 —— 用户可能只是漏了课表没填，
      重新打开引导页时不该让他把账号再打一遍。 */
  suPrefill();
  suPageSet(page || 1, {force: true});
  setTimeout(()=>{ try{ $('#suUser')?.focus(); }catch(e){} }, 320);
}
function closeSetup(){
  const el = $('#setup');
  if(el) el.classList.remove('open');
  document.body.classList.remove('setting-up');
}

/* 把后端已经存下的账号回填到对应页 —— 少打一遍字 */
async function suPrefill(){
  try{
    const s = await apiSetupStatus();
    if(!s) return;
    const put = (sel, v)=>{
      const el = $(sel);
      if(el && !el.value && v) el.value = v;
    };
    put('#suUser', s.mb_user);
    put('#suUrl', s.mb_host);
    put('#suSchUser', s.sch_user);
    put('#suEc', (s.ec_names || []).join('、'));
  }catch(e){}
}

/* 「跳过」按钮的处理。

   ★ 设计上的一条规则：**跳过的语义是「这一页不填」**。
     所以如果用户其实已经填了，那就不算跳过 —— 直接按「保存并继续」走
     （复用 suSubmitSchedule，有「正在保存…」反馈、失败会停在本页报错）。

     早先这里自己写了一遍保存逻辑，结果是：
       · 点跳过时没有任何反馈，用户不知道是不是在存
       · 保存失败也照样往下走，内容悄悄丢了
       · 同一段逻辑写两遍，将来改一处漏一处
     现在把它交给 suSubmitSchedule，一处逻辑一处改。

   第 2 页（课表）：填了就存，没填就翻下一页。
   第 3 页（EC）：填了就存 + 收尾，没填就直接收尾。 */
async function suSkip(n){
  if(n === 2){
    const u = ($('#suSchUser')?.value || '').trim();
    const p = ($('#suSchPw')?.value || '');
    if(u || p){
      /* 填了 → 走「保存并继续」（它自己会翻到第 3 页） */
      await suSubmitSchedule();
      return;
    }
    suPageSet(3);
    return;
  }
  if(n === 3){
    const ec = ($('#suEc')?.value || '').trim();
    if(ec){
      await suSubmitEc();
      return;
    }
    await suFinish('');
    return;
  }
  /* 第 1 页没有跳过（账号必填）—— 保持一致：直接进下一页 */
  suPageSet(n + 1);
}

/* 收尾：装自启 + 进主界面 + 抓一次 */
async function suFinish(ecNames){
  /*  顺手把开机预热装上 —— 这一步是「装好即用」的最后一块：
        装完之后，以后开机它自己在后台把数据抓好，
        点开小组件立刻有内容，不用等。
      失败也不拦着用户（有些环境不允许写启动文件夹），
      只记下来，设置面板里还能手动开。 */
  try{
    const a = await apiAutostartInstall();
    AUTOSTART_ON = !!(a && a.ok);
  }catch(e){ AUTOSTART_ON = false; }

  suMsg('ok', icon('i-check','sm inl')
    + (AUTOSTART_ON
        ? '全部搞定！以后开机会自动更新，打开即用。'
        : '全部搞定！正在读取你的作业和成绩…'));

  HAS_CREDENTIALS = true;      /*  填完了，之后不再显示引导 */
  setTimeout(()=>{ closeSetup(); }, 700);
  lastSig = '';
  poll();
  try{ await apiRefresh(); }catch(e){}
  HAS_CREDENTIALS = true;
}

/* 判断是否需要引导页，需要就打开。
    抽成函数（不直接写在 DOMContentLoaded 里）的理由：
     预览页 / pywebview 可能在 DOMContentLoaded **之后**才注入脚本，
     那种情况下监听器永远不会触发 —— 这是在浏览器里验证时真实踩到的坑。 */
async function bootSetup(){
  try{
    /*   打包版实测踩中：pywebview 的 api 是**页面加载完之后**才注入的
        （见 reportReady 的说明），bootSetup 在初始化时同步就跑，
        此刻 window.pywebview 多半还没出现 → apiSetupStatus 落到
        网页模式兜底值 {need_setup:false} → **引导页永远不弹**，
        新用户第一次打开看到的是空看板而不是三步引导。
        修法：先等桥出现（最多 3 秒）再问。
        网页版（app/server.py）永远没有桥，等 3 秒无害 ——
        那里 need_setup 本来就是 false。注意不能用 IS_WEB 区分：
        打包版走 pywebview 内置 http 服务，协议也是 http。 */
    if(!(window.pywebview && window.pywebview.api)){
      await new Promise(res => {
        let done = false;
        const finish = ()=>{ if(!done){ done = true; res(); } };
        window.addEventListener('pywebviewready', finish, {once:true});
        const t0 = Date.now();
        (function poll(){
          if(done) return;
          if(window.pywebview && window.pywebview.api) return finish();
          if(Date.now() - t0 > 3000) return finish();
          setTimeout(poll, 100);
        })();
      });
    }
    const s = await apiSetupStatus();
    if(s){
      /*  记住「有没有填过账号」——
         主界面的空状态要用它决定是「去填账号」还是「重新登录」 */
      HAS_CREDENTIALS = !s.need_setup;
      if(s.need_setup) openSetup();
    }
  }catch(e){ /* 拿不到状态就不打扰用户，直接进主界面 */ }
}

let suBusy = false;

/* 点击「登录」 */
async function suSubmit(){
  if(suBusy) return;

  const host = suHostOf($('#suUrl')?.value) || suHostOf(DEFAULT_HOST || '');
  const user = ($('#suUser')?.value || '').trim();
  const pw   = ($('#suPw')?.value || '');

  /* ── 本地校验（不发请求就能发现的问题先挡掉）── */
  ['#suUser','#suPw','#suUrl'].forEach(s=>{
    const el = $(s); if(el) el.classList.remove('bad');
  });
  /*  网址为空或还是占位域名 → 不发了，发了也是「无法连接」。
      顺手把折叠区掀开（默认已开，这里是兜底）并聚焦过去。 */
  if(!host || host === SETUP_PLACEHOLDER_HOST){
    $('#suUrl')?.classList.add('bad');
    document.getElementById('suUrlFold')?.classList.add('open');
    suMsg('err', icon('i-alert','sm inl') + '请把学校网址改成你学校的域名');
    try{ $('#suUrl')?.focus(); }catch(e){}
    return;
  }
  if(!user){
    $('#suUser')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填账号');
    try{ $('#suUser')?.focus(); }catch(e){}
    return;
  }
  if(!pw){
    $('#suPw')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填密码');
    try{ $('#suPw')?.focus(); }catch(e){}
    return;
  }

  suBusy = true;
  const btn = $('#suGo');
  const oldHtml = btn ? btn.innerHTML : '';
  if(btn){
    btn.disabled = true;
    btn.innerHTML = icon('i-refresh','sm spin') + '<span>正在登录…</span>';
  }
  suMsg('info', '正在连接，大约 10 秒。');

  const fullUrl = 'https://' + host;

  try{
    /* ── 第一步：先测试（ 不保存，避免把错的凭据留在磁盘）── */
    const r = await apiTestLogin(user, pw, fullUrl);

    if(!r || !r.ok){
      const why = (r && r.error) || '登录失败';
      suMsg('err', icon('i-alert','sm inl') + esc(suFriendly(why)));
      $('#suUser')?.classList.add('bad');
      $('#suPw')?.classList.add('bad');
      return;
    }

    /* ── 第二步：测试通过才保存 ── */
    suMsg('info', '正在保存…');
    const s = await apiSaveSetup(user, pw, fullUrl);
    if(!s || !s.ok){
      suMsg('err', icon('i-alert','sm inl')
        + esc((s && s.error) || '保存失败'));
      return;
    }

    /*  ★ 存好了 → 翻到第 2 页（课表）。
          不在这里做「装自启 + 抓取」，那些放到最后一页收尾 ——
          不然用户还在填课表，后台就开始抓了，
          而且自启装了之后万一课表填错也没机会改。 */
    suMsg('ok', icon('i-check','sm inl') + '学校系统好了。');
    setTimeout(()=>{ suPageSet(2); }, 420);

  }catch(e){
    suMsg('err', icon('i-alert','sm inl')
      + esc('出错了：' + (e && e.message ? e.message : e)));
  }finally{
    suBusy = false;
    if(btn){
      btn.disabled = false;
      btn.innerHTML = oldHtml;
    }
  }
}

/* 第 2 页：保存课表账号（希悦校园）。
   ★ 课表是**另一个网站**，账号形态也不同（手机号）。
     存失败不拦用户往下走 —— 课表是可选的。 */
async function suSubmitSchedule(){
  if(suBusy) return;
  const u = ($('#suSchUser')?.value || '').trim();
  const p = ($('#suSchPw')?.value || '');

  ['#suSchUser','#suSchPw'].forEach(s=>{
    const el = $(s); if(el) el.classList.remove('bad');
  });
  if(!u && !p){
    /* 什么都没填 → 当跳过处理 */
    suPageSet(3);
    return;
  }
  if(!u){
    $('#suSchUser')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填手机号');
    return;
  }
  if(!p){
    $('#suSchPw')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填密码');
    return;
  }

  suBusy = true;
  const btn = $('#suGo2');
  const oldHtml = btn ? btn.innerHTML : '';
  if(btn){
    btn.disabled = true;
    btn.innerHTML = icon('i-refresh','sm spin') + '<span>正在保存…</span>';
  }
  suMsg('info', '正在保存课表账号…');
  try{
    const r = await apiSaveSchedule(u, p);
    if(!r || !r.ok){
      suMsg('err', icon('i-alert','sm inl')
        + esc((r && r.error) || '保存失败'));
      return;
    }
    suMsg('ok', icon('i-check','sm inl') + '课表账号好了。');
    setTimeout(()=>{ suPageSet(3); }, 420);
  }catch(e){
    suMsg('err', icon('i-alert','sm inl') + esc('出错了：' + e));
  }finally{
    suBusy = false;
    if(btn){
      btn.disabled = false;
      btn.innerHTML = oldHtml;
    }
  }
}

/* 第 3 页：EC 名字 + 收尾 */
async function suSubmitEc(){
  if(suBusy) return;
  const ec = ($('#suEc')?.value || '').trim();
  suBusy = true;
  const btn = $('#suGo3');
  const oldHtml = btn ? btn.innerHTML : '';
  if(btn){
    btn.disabled = true;
    btn.innerHTML = icon('i-refresh','sm spin') + '<span>保存中…</span>';
  }
  try{
    if(ec){
      const r = await apiEcSave(ec, true);
      if(r && !r.ok){
        suMsg('err', icon('i-alert','sm inl')
          + esc((r && r.error) || '保存失败'));
        return;
      }
    }
    await suFinish(ec);
  }catch(e){
    suMsg('err', icon('i-alert','sm inl') + esc('出错了：' + e));
  }finally{
    suBusy = false;
    if(btn){
      btn.disabled = false;
      btn.innerHTML = oldHtml;
    }
  }
}

/* 把技术错误翻译成普通人能懂的话
    绝不说「请检查 credentials.json」这种 —— 用户不知道那是什么 */
function suFriendly(raw){
  const s = String(raw || '');
  if(/锁定|locked|consecutive/i.test(s)){
    return '账号被临时锁定，请等 30 分钟后再试。';
  }
  if(/invalid.*(email|password)|密码|凭据|credential/i.test(s)){
    return '账号或密码不正确。';
  }
  if(/429|太多|too many/i.test(s)){
    return '请求过于频繁，请稍后再试。';
  }
  if(/连接|connect|timeout|超时|getaddrinfo|Name or service/i.test(s)){
    return '无法连接服务器，请检查网址和网络。';
  }
  if(/ssl|certificate|证书/i.test(s)){
    return '该网址的 HTTPS 证书无效，请确认网址是否正确。';
  }
  return s || '登录失败。';
}

/* 绑定引导页的所有交互 */
function bindSetup(){
  /*  幂等：只能绑一次。
      踩过的坑：页面里有两个 DOMContentLoaded 监听器都会调到这里，
      第二次进来 buildSetup() 重建 innerHTML → 旧节点连同监听器一起丢 →
      表现为「按钮点了没反应」，而且不报任何错。 */
  if(bindSetup._done) return;
  bindSetup._done = true;

  buildSetup();

  /*  眼睛按钮：账号页 + 课表页各一个。
      ★ 抽成函数，别复制两遍 —— 复制出来的那份迟早会漏改。 */
  function bindEye(eyeSel, pwSel){
    const eye = $(eyeSel), pw = $(pwSel);
    if(!eye || !pw) return;
    eye.addEventListener('click', e=>{
      e.stopPropagation();
      const show = pw.type === 'password';
      pw.type = show ? 'text' : 'password';
      eye.innerHTML = icon(show ? 'i-eye-off' : 'i-eye', 'sm');
    });
  }
  bindEye('#suEye', '#suPw');
  bindEye('#suSchEye', '#suSchPw');

  /*  学校网址折叠：用事件委托挂在 document 上。
      比直接绑 #suUrlHead 稳 —— 那个节点将来要是被重建，
      委托不会跟着失效。 */
  document.addEventListener('click', e=>{
    const t = e.target;
    if(t && t.closest && t.closest('#suUrlHead')){
      e.preventDefault();
      e.stopPropagation();
      document.getElementById('suUrlFold')
        ?.classList.toggle('open');
    }
  }, true);

  /* 状态条里的「立即刷新」*/
  (function bindMsgRetry(){
    const rt = document.getElementById('msgRetry');
    if(!rt || rt.dataset.bound) return;
    rt.dataset.bound = '1';
    rt.addEventListener('click', async e=>{
      e.stopPropagation();
      /*  先自己进 busy（后端状态要下一次轮询才会变成 running）——
          不然用户会在这半秒里连点好几下 */
      rt.classList.add('busy');
      try{ await apiRefresh(); }catch(err){}
    });
  })();

  $('#suGo')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSubmit();
  });
  $('#suGo2')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSubmitSchedule();
  });
  $('#suGo3')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSubmitEc();
  });
  $('#suSkip2')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSkip(2);
  });
  $('#suSkip3')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSkip(3);
  });
  $('#suSkip1')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSkip(1);
  });

  /* 回车提交 —— 每页的回车落在**自己那页**的提交上，
     别让课表页的回车跑到账号页去提交。 */
  const ENTER_MAP = [
    ['#suUser',    suSubmit],
    ['#suPw',      suSubmit],
    ['#suUrl',     suSubmit],
    ['#suSchUser', suSubmitSchedule],
    ['#suSchPw',   suSubmitSchedule],
    ['#suEc',      suSubmitEc],
  ];
  ENTER_MAP.forEach(([sel, fn])=>{
    const el = $(sel);
    if(!el) return;
    el.addEventListener('keydown', e=>{
      if(e.key === 'Enter'){ e.preventDefault(); fn(); }
    });
    /* 一开始输入就清掉红色标记，别让用户一直看着报错 */
    el.addEventListener('input', ()=>{
      el.classList.remove('bad');
      suClearMsg();
    });
  });
}

/* ══════════ 事件 ══════════ */
function bind(){
  document.querySelectorAll('[data-tg]').forEach(el=>{
    el.addEventListener('click',()=>{
      const id = el.dataset.tg;
      collapsed[id] = !collapsed[id];
      localStorage.setItem('mb.col', JSON.stringify(collapsed));
      el.closest('.sec').classList.toggle('collapsed', collapsed[id]);
    });
  });
  document.querySelectorAll('[data-ch]').forEach(el=>{
    el.addEventListener('click',()=>{
      const id = el.dataset.ch;
      openCourses[id] = !openCourses[id];
      localStorage.setItem('mb.oc', JSON.stringify(openCourses));
      el.closest('.course').classList.toggle('open', openCourses[id]);
    });
  });
  document.querySelectorAll('[data-url]').forEach(el=>{
    el.addEventListener('click',e=>{
      e.stopPropagation();
      /* 任务卡片 → 展开详情面板（用户要求）
         其他元素（课程明细行等）→ 直接跳转原网页 */
      if(el.classList.contains('card')){
        toggleDetail(el.dataset.url, el);
      }else{
        openURL(el.dataset.url);
      }
    });
  });
  /* 下载按钮 */
  document.querySelectorAll('.dlbtn[data-dl]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const [cid, tid, title] = btn.dataset.dl.split('|');
      if(btn.classList.contains('busy')) return;
      btn.classList.add('busy');
      btn.innerHTML = icon('i-refresh','sm');
      toast('正在准备下载……', '', '');
      Promise.resolve(apiDownloadTask(cid, tid, title)).finally(()=>{
        setTimeout(()=>{ btn.classList.remove('busy');
                         btn.innerHTML = icon('i-download','sm'); }, 900);
      });
    });
  });

  /* 「我已完成，不再提示」 */
  document.querySelectorAll('.mkbtn[data-mk]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const card = btn.closest('.card');
      const url = card?.dataset.url || '';
      markDone(url, btn);
    });
  });
  /* 撤销手动标记 */
  document.querySelectorAll('.mkbtn[data-unmk]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const card = btn.closest('.card');
      const url = card?.dataset.url || '';
      unmarkDone(url, btn);
    });
  });

  /*  资料（Files）按钮 —— 0.5.2：直接跳到「资料」页并定位到这门课。
     以前是在卡片里内嵌一个文件浏览器（读一遍 DOM、再渲染一遍），
     两套实现并存容易改一处漏一处，现在只留「资料」页那一套。 */
  document.querySelectorAll('.fbtn[data-files]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const cid = btn.dataset.files.split('|')[0];
      FSTATE.cid = cid;
      FSTATE.fid = '';
      FSTATE.fname = '';
      FSTATE.data = null;
      switchTab(3);
      filesLoad('', true, '');
    });
  });
  /* 全部下载 */
  document.querySelectorAll('.fbtn[data-dlall]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const [cid, name] = btn.dataset.dlall.split('|');
      toast('正在下载「' + name + '」的全部资料……', '', '');
      Promise.resolve(apiDownloadFiles(cid, name));
    });
  });
}

/* ══════════ Files 浏览 ══════════ */

/*  文件名 → 图标 id */
/* ══════════ 按钮 ══════════
/*  ★ 「立即刷新」和「数据与备份」已经从标题栏挪到设置面板底部 ——
      标题栏原来 11 个按钮，小窗口下要横向滚才放得下。
      这里改用**事件委托**：不管在哪个容器里，
      只要 id 对得上就绑上（读的是 document，不依赖具体位置）。 */
function bindOpButtons(){
  const rf = document.getElementById('opRefresh');
  if(rf && !rf.dataset.bound){
    rf.dataset.bound = '1';
    rf.addEventListener('click', ()=>{
      rf.classList.add('spin');
      apiRefresh();
      /*  刷新按钮的「转圈」状态本来由 render() 按 DATA.status 控制，
          但那绑的是老 id；这里给个兜底，转 1.2 秒就停。 */
      setTimeout(()=> rf.classList.remove('spin'), 1200);
    });
  }
  const dt = document.getElementById('opData');
  if(dt && !dt.dataset.bound){
    dt.dataset.bound = '1';
    dt.addEventListener('click', ()=> openData());
  }
  /*  0.5.1 从标题栏收进来的两个：暂停自动刷新、重新登录。
      逻辑抽成了 doToggleAuto / doRelogin，谁绑都一样。 */
  const au = document.getElementById('opAuto');
  if(au && !au.dataset.bound){
    au.dataset.bound = '1';
    au.addEventListener('click', doToggleAuto);
  }
  const lg = document.getElementById('opLogin');
  if(lg && !lg.dataset.bound){
    lg.dataset.bound = '1';
    lg.addEventListener('click', ()=> doRelogin(lg));
  }
}
/*  设置面板在文件后面才解析出来，得等 DOM ——
    同 bootSettingsMini 那个坑（见那里的说明）。 */
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', bindOpButtons);
}else{
  setTimeout(bindOpButtons, 0);
}
setTimeout(bindOpButtons, 800);

/*  暂停 / 开启自动刷新 —— 0.5.1 起从标题栏收进设置「操作」区（opAuto）。
    标题栏上的 #live 倒计时还在，暂停时显示 ||，状态照样看得见。 */
function doToggleAuto(){
  const on = !(DATA && DATA.auto_refresh);
  apiSetAuto(on);
  if(DATA) DATA.auto_refresh = on;
  render();
}
/* ══════════ 重新登录 ══════════
   用户要求：「不要让我自己运行某个 python 程序」。 */
function apiRelogin(){
  if(window.pywebview?.api?.relogin) return window.pywebview.api.relogin();
  return Promise.resolve({ok:false});
}

/* 界面上所有带 data-relogin 的按钮 → 都触发自动登录 */
function bindRelogin(root){
  (root || document).querySelectorAll('[data-relogin]').forEach(btn=>{
    if(btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      if(btn.classList.contains('busy')) return;
      btn.classList.add('busy');

      const old = btn.innerHTML;
      btn.innerHTML = icon('i-refresh','sm') + '<em>正在登录…</em>';
      toast('正在自动登录，请稍候……', '', '');

      Promise.resolve(apiRelogin()).then(res=>{
        if(res && res.ok === false){
          toast('启动登录失败：' + (res.error || '未知原因'), 'err', '');
        }else{
          toast('已开始登录，完成后会自动刷新', '', '');
        }
        /* 登录是后台跑的，这里先恢复按钮，稍后 poll 会自动刷新数据 */
        setTimeout(()=>{
          btn.classList.remove('busy');
          btn.innerHTML = old;
        }, 2600);
      }).catch(err=>{
        btn.classList.remove('busy');
        btn.innerHTML = old;
        toast('登录失败：' + err, 'err', '');
      });
    });
  });
}

document.addEventListener('DOMContentLoaded',()=>{
  /*  网页模式下没有真实的窗口可管 ——
      最小化/最大化/关闭这组按钮藏起来，
      否则点了没反应，看着像坏了。
      （判定在 syncWinbtnsVisibility 里，桥到了会自己纠正。） */
  syncWinbtnsVisibility();

  /*  首次运行：看有没有填过账号。
     没填过 → 弹出引导页（盖住主界面）；
     填过   → 什么都不做，直接进主界面。 */
  bindSetup();
  bootSetup();
  checkDbHealth();
  checkBrowserHealth();
  bindCustomPanel();
  bindSettings();
  bindSearch();
  bindOpenDialog();
  bindTeams();


  pollWarm();
  refreshDigestDot();
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1000);
  if(tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(tick, 1000);
  if(warmTimer) clearInterval(warmTimer);
  warmTimer = setInterval(pollWarm, 30000);
  if(digTimer) clearInterval(digTimer);
  /* 晚报状态变化很慢（一天一次），低频轮询就够 */
  digTimer = setInterval(refreshDigestDot, 120000);
});

/* 本地关键状态体检：成绩历史库 + 凭据文件。
 *
 * · grades.db 是成绩历史的**唯一副本** —— 没有它就画不了趋势、
 *   也回不到过去某天的分数。以前库里写失败只写 debug 日志（默认看不到）。
 * · credentials.json 一旦读不出来，用户会莫名其妙回到首次引导页，
 *   看起来像「账号丢了」。
 * 两处都不该让用户在无解释的情况下自己猜，所以启动时问一次，坏了就明说。 */
async function checkDbHealth(){
  try{
    if(!(window.pywebview?.api?.db_health)) return;
    const h = await window.pywebview.api.db_health();
    if(h && h.ok === false){
      toast('本地数据有问题：' + (h.note || '未知原因'), 'err');
    }
  }catch(e){ /* 体检本身不该影响启动 */ }
}

/*  ★ 浏览器依赖体检（0.5.2 补）
    为什么值得开机就问一次：Chromium 内核浏览器（跳转 + Teams + EC + 附件下载）
    和 WebView2 运行时装（界面渲染）是**两个独立组件**。
    「有 WebView2 没有浏览器」的机器上，界面照常打开、什么都不报错，
    但上面那些功能会一起失灵 —— 用户只会觉得「这软件坏了」。
    Edge / Chrome / Brave / Chromium 任意一个都行，缺了才报。
    这里缺什么就明说，并且只提醒一次（免得每次启动都弹）。 */
async function checkBrowserHealth(){
  try{
    if(!(window.pywebview?.api?.browser_health)) return;
    const h = await window.pywebview.api.browser_health();
    if(h && h.ok === false && h.message){
      if(sessionStorage.getItem('bhwarn') === '1') return;
      sessionStorage.setItem('bhwarn','1');
      toast(h.message, 'err');
    }
  }catch(e){ /* 体检本身不该影响启动 */ }
}

/* 界面上所有带 data-opensetup 的按钮 → 打开首次运行引导页
   （用于「还没填过账号」时的空状态引导） */
function bindOpenSetup(root){
  (root || document).querySelectorAll('[data-opensetup]').forEach(btn=>{
    if(btn.dataset.osbound) return;
    btn.dataset.osbound = '1';
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      openSetup();
    });
  });
}

/*  重新登录（自动）—— 0.5.1 起从标题栏收进设置「操作」区（opLogin）。
    用户要求：「不要让我自己运行某个 python 程序」。
    没填过账号 → 打开引导页让他填；填过了 → 后台自动登录。 */
function doRelogin(b){
  if(!HAS_CREDENTIALS){ openSetup(); return; }
  if(b && b.classList.contains('spin')) return;    /* 防重复点 */

  if(b){ b.classList.add('spin'); }
  toast('正在自动恢复登录状态……（两个系统一起）', '', '');

  Promise.resolve(apiRelogin()).then(res=>{
    if(res && res.ok === false){
      toast('启动登录失败：' + (res.error || '未知原因'), 'err', '');
    }else{
      toast('已开始自动登录，完成后会自动刷新', '', '');
    }
  }).catch(e=>{
    toast('登录失败：' + e, 'err', '');
  }).finally(()=>{
    /* 登录是后台跑的，先恢复按钮外观；数据会自己刷新 */
    setTimeout(()=>{ if(b) b.classList.remove('spin'); }, 4000);
  });
}
/* ── 晚报：点顶部信封按钮展开 ── */
$('#btnDigest').addEventListener('click', openDigest);
/* ── 数据与备份：入口在设置面板里（见 bindOpButtons） ── */

/* ══════════════════════════════════════════════════════════════════
   窗口菜单（ESC 唤出）

   ★ 为什么要有：窗口是无边框的，关窗/最小化全靠界面上画的按钮。
     万一那几个按钮点不到（界面卡住、鼠标拖不动、或者就是想少点两下），
     窗口就变成一个关不掉的方块。Esc 唤出一层重模糊遮罩 +
     三个大按钮，是第二道保险。

   ★ Esc 的处理顺序（由内到外，一层层退）：
       ① 窗口菜单开着   → 关掉菜单
       ② 有面板开着     → 关掉那个面板
       ③ 在输入框里     → 只是取消聚焦
       ④ 什么都没开     → 唤出窗口菜单
     顺序不能乱：先关最上层的，最后才轮到唤出 ——
     否则菜单会把用户正在看的设置面板盖住。
   ══════════════════════════════════════════════════════════════════ */

function winMenuEl(){ return document.getElementById('winmenu'); }

function openWinMenu(){
  const el = winMenuEl();
  if(!el) return;
  syncWinMenu();
  el.classList.add('open');
}

function closeWinMenu(){
  const el = winMenuEl();
  if(el) el.classList.remove('open');
}

function winMenuOpen(){
  const el = winMenuEl();
  return !!(el && el.classList.contains('open'));
}

/*  菜单里那个「最大化 / 还原」要按当前状态显示 ——
    写着「最大化」却已经最大了，点下去看不出变化，会让人以为坏了。 */
function syncWinMenu(){
  const b = document.getElementById('wmMax');
  if(!b) return;
  const use = b.querySelector('.wm-ic use');
  const t = b.querySelector('.wm-tx b');
  if(use) use.setAttribute('href', WIN_MAXED ? '#i-winrest' : '#i-winmax');
  if(t) t.textContent = WIN_MAXED ? '还原' : '最大化';
}

/*  Esc 总入口 */
document.addEventListener('keydown', e=>{
  if(e.key !== 'Escape') return;

  /*  ⓪ 教程开着 → 关掉教程。
      ★ 它排在最前面，因为它是 z-index 最高的那一层
        （950，比窗口菜单的 999 低但比面板的 900 高 ——
         而且 openTour() 会先把窗口菜单和面板都关掉，
         所以教程开着的时候不会有别的东西叠在它上面）。
      按 Esc 退掉 = 用户不想看，直接记「看过了」，
      不然下次启动又冒出来。 */
  if(document.getElementById('tour')?.classList.contains('open')){
    e.stopPropagation();
    e.preventDefault();
    closeTour(true);
    return;
  }

  /*  ① 窗口菜单开着 → 关掉它（最高优先级） */
  if(winMenuOpen()){
    e.stopPropagation();
    e.preventDefault();
    closeWinMenu();
    return;
  }

  /*  ② 有面板开着 → 关掉那个面板（沿用原来的行为） */
  const panels = [
    ['#digest',    ()=> closeDigest()],
    ['#datapanel', ()=> closeData()],
    ['#setpanel',  ()=> closeSettings()],
    ['#teampanel', ()=> closeTeams()],
    ['#clpanel',   ()=> closeCustomPanel()],
  ];
  for(const [sel, fn] of panels){
    const p = $(sel);
    if(p && p.classList.contains('open')){
      e.stopPropagation();
      fn();
      return;
    }
  }

  /*  ③ 焦点在输入框里 → 只取消聚焦，不弹菜单 */
  const t = e.target;
  if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
           || t.tagName === 'SELECT')){
    try{ t.blur(); }catch(err){}
    return;
  }

  /*  ④ 什么都没开 → 唤出窗口菜单 */
  e.preventDefault();
  openWinMenu();
}, true);

/* 菜单上的四个按钮 */
$('#wmMin')?.addEventListener('click', ()=>{
  winApi('minimize');
  closeWinMenu();
});
$('#wmMax')?.addEventListener('click', async ()=>{
  const r = await winApi('win_maximize');
  if(r && r.state) WIN_MAXED = (r.state === 'max');
  else WIN_MAXED = !WIN_MAXED;
  syncMaxIcon();
  syncWinMenu();
  closeWinMenu();
});
$('#wmClose')?.addEventListener('click', ()=>{
  closeWinMenu();
  winApi('win_close');
});
$('#wmQuit')?.addEventListener('click', ()=>{
  winApi('win_quit');
});

/*  点遮罩（卡片外面）也关掉菜单。
    判断 e.target 就是遮罩本身 —— 点卡片内部不该关。 */
winMenuEl()?.addEventListener('click', e=>{
  if(e.target && e.target.id === 'winmenu') closeWinMenu();
});

/*  标题栏那个 MB 图标也能唤出菜单 ——
    Esc 是给「知道的人」用的，图标是给「不知道的人」看的。 */
document.getElementById('bar')?.addEventListener('click', e=>{
  if(e.target.closest('.logo')) openWinMenu();
});
(function hintLogo(){
  const lg = document.querySelector('#bar .logo');
  if(lg) lg.title = '窗口操作（也可按 Esc）';
})();

/* ══════════════════════════════════════════════════════════════════
   窗口按钮（最小化 / 最大化 / 关闭）

   为什么要有这组按钮：
     窗口是无边框的 —— 没有系统标题栏，也就没有系统的
     最小化/最大化/关闭。不给这组按钮，窗口就关不掉了。

   三个按钮都在右上角同一区域，和 Windows 上别的程序位置一致。
   ══════════════════════════════════════════════════════════════════ */
function winApi(name){
  const a = window.pywebview && window.pywebview.api;
  return (a && typeof a[name] === 'function') ? a[name]() : null;
}

/* 最大化 / 还原。图标跟着状态换 —— 点完要是还显示「方框」，
   用户就不知道再点一下会还原。 */
let WIN_MAXED = false;

function syncMaxIcon(){
  const b = document.querySelector('#btnWinMax .oi use');
  if(b) b.setAttribute('href', WIN_MAXED ? '#i-winrest' : '#i-winmax');
  const bar = document.getElementById('bar');
  if(bar) bar.classList.toggle('maxed', WIN_MAXED);
  const btn = document.getElementById('btnWinMax');
  if(btn) btn.title = WIN_MAXED ? '还原' : '最大化';
}

/*  0.5.2：窗口只有「最大化」与「最小化」两种状态，
    最大化/还原按钮已从标题栏删除，这里对应地只保留最小化与关闭。
    绑定一律先判存在 —— 元素被删掉时不能抛异常，
    否则整段脚本停在这里，后续所有绑定都会失效。 */
const _btnMin = $('#btnWinMin');
if(_btnMin) _btnMin.addEventListener('click', ()=>{
  winApi('minimize');
});

const _btnClose = $('#btnWinClose');
if(_btnClose) _btnClose.addEventListener('click', ()=>{
  winApi('win_close');
});

/*  双击标题栏曾经是「最大化 / 还原」。现在窗口恒为最大化，
    这个动作没有意义，改成不做任何事（保留监听以免影响拖动判定）。 */
const _bar = $('#bar');
if(_bar) _bar.addEventListener('dblclick', (e)=>{
  if(e.target.closest('button')) return;
});

/* ══════════ 自绘拖动（0.5.1）══════════
   ★ 为什么不用 pywebview 内建拖动（.pywebview-drag-region）了：
     那条链路（内建 http 分发 + 无边框 + WinForms）实测不生效 ——
     合成事件和真鼠标都试过，window.move 调了窗口纹丝不动。
     内建机制留着也无害（类名还在 HTML 上），但真正拖窗的是这里。

   做法：#bar 上按下左键 → mousemove 算屏幕坐标增量 →
   后端 win_drag(dx, dy) 走 SetWindowPos。
     · 点在按钮 / logo / 链接上不拖（它们有自己的点击功能）
     · 增量用 screenX（CSS 像素），DPI 换算在后端做
     · 桥调用节流：每帧最多送一次，攒着的增量一起送 */
(function selfDrag(){
  const bar = document.getElementById('bar');
  if(!bar) return;
  let on = false, lastX = 0, lastY = 0, pendX = 0, pendY = 0, sent = false;
  /*  把攒下的增量送出去；桥调用在飞时先攒着，回来后再送 —
      注意 finally 里要再补一次 flush，不然「最后一次移动
      到鼠标松开」之间的增量会丢（窗口差几像素不到位）。 */
  function flush(){
    if(sent || (!pendX && !pendY)) return;
    const a = window.pywebview && window.pywebview.api;
    if(!(a && typeof a.win_drag === 'function')) return;
    sent = true;
    const dx = pendX, dy = pendY;
    pendX = 0; pendY = 0;
    Promise.resolve(a.win_drag(dx, dy))
      .catch(()=>{})
      .finally(()=>{ sent = false; flush(); });
  }
  bar.addEventListener('mousedown', e=>{
    if(e.button !== 0) return;
    if(e.target.closest('button, a, input, select, .logo')) return;
    on = true; sent = false;
    lastX = e.screenX; lastY = e.screenY;
    pendX = 0; pendY = 0;
    e.preventDefault();              /* 别进入文字选择/滚动 */
  });
  window.addEventListener('mousemove', e=>{
    if(!on) return;
    pendX += e.screenX - lastX;
    pendY += e.screenY - lastY;
    lastX = e.screenX; lastY = e.screenY;
    flush();
  });
  window.addEventListener('mouseup', ()=>{ on = false; });
})();

/*  记住窗口尺寸 —— 用户拖过大小之后，下次启动照旧。
    监听 resize，但拖拽过程中会连续触发，所以节流。 */
let _rszT = null;
window.addEventListener('resize', ()=>{
  clearTimeout(_rszT);
  _rszT = setTimeout(()=>{
    /*  最大化状态下尺寸跟着屏幕走，不要存成「用户偏好」。
        ★ 0.5.1：判断挪到保存**之前** —— 以前是存完才判断，
          启动即最大化会把整个屏幕的尺寸写进 full_size。 */
    const maxed = window.outerWidth >= screen.availWidth - 4;
    if(!maxed){
      const a = window.pywebview && window.pywebview.api;
      if(a && typeof a.win_resize === 'function'){
        a.win_resize(window.outerWidth || 0, window.outerHeight || 0);
      }
    }
    WIN_MAXED = maxed;
    syncMaxIcon();
  }, 500);
});

/*  ★ 启动即最大化的场景：后端在页面加载完之前就放大了窗口，
    resize 监听没赶上这趟车，WIN_MAXED 还停在 false ——
    图标会一直显示「方框」（其实已经是最大化），
    用户就不知道再点一下能还原。第一次同步前先量一次真实尺寸。 */
WIN_MAXED = window.outerWidth >= screen.availWidth - 4;
syncMaxIcon();

/*  ★ 向后端报个到：「前端起来了，标题栏按钮已就位」。
    后端拿这个当**安全网** —— 无边框窗口没有系统关闭键，
    界面要是没跑起来（比如某个 JS 出错），窗口就关不掉了。
    超时没报到的话，后端会主动把系统边框装回去。

    ★ 时机很重要：pywebview 的 api 对象是**页面加载完之后**
      才注入的，所以这里不能直接调 —— 要等它出现。
      用 pywebviewready 事件（最准），再加一个轮询兜底
      （网页模式下没这个事件，轮询也不会误报）。 */
function reportReady(){
  if(reportReady._done) return;
  const a = window.pywebview && window.pywebview.api;
  if(!a || typeof a.win_ready !== 'function') return;
  reportReady._done = true;
  try{ a.win_ready(); }catch(e){}
}
window.addEventListener('pywebviewready', reportReady);
/*  兜底：每 300ms 看一次，最多 40 次（12 秒）。
    和后端的 READY_TIMEOUT 对齐。 */
(function pollReady(n){
  if(reportReady._done || n <= 0) return;
  reportReady();
  if(!reportReady._done) setTimeout(()=>pollReady(n - 1), 300);
})(40);


$('#btnFold').addEventListener('click',()=>{
  const ids=['tasks','quiz','hw','other','courses','done',
             'sch-today','sch-tmr','sch-rest'];
  const anyOpen = ids.some(i=>!collapsed[i]);
  ids.forEach(i=>collapsed[i]=anyOpen);
  localStorage.setItem('mb.col', JSON.stringify(collapsed));
  lastSig = '';
  render();
});
$('#live').addEventListener('click',()=>{
  lastSig = '';
  poll();
});

/* ══════════════════════════════════════════════════════════════════
   动画节流兜底：同一元素 0.7 秒内不允许重播动画

   ★ 为什么要有这个
     即使上面两处都加了签名比对，将来新加的元素仍可能踩同样的坑
     （「每秒重建 + 元素上有动画」）。这是**结构性问题**，
     而不是某两处的 bug。

     这里加一道全局兜底：监听 animationstart，如果同一元素
     在 0.7 秒内又要开始动画，就把它取消掉。

   ★ 为什么是 0.7 秒
     · 正常动效时长 0.28~0.42 秒，播完后 0.7 秒内不会再重播
     · 每秒重播的情况会被挡掉（间隔 1 秒 > 0.7 秒？不，是挡住）
       实际逻辑：记录上次**开始**时间，间隔 < 0.7s 就拦。
       每秒一次的间隔是 1.0s，比 0.7s 长……
       ★ 所以还要看「动画时长」：如果动效本身没播完就重播，那才是鬼畜。
       这里改成判断「上次动画是否已结束」——
       没结束又重播 = 鬼畜，直接取消。

   ★ 会不会误伤正常动效
     不会。正常动效每次都能播完（用户主动触发，间隔远大于时长）。
     只有「上一轮还没播完就被重建」才会被拦。
     另外给用户主动触发的动效开了白名单（hover/click 类）。
   ══════════════════════════════════════════════════════════════════ */
const _animGuard = new WeakMap();      /* element -> 上次开始时间 */
const _animAllow = new WeakSet();      /* 用户主动触发的动画 */

document.addEventListener('animationstart', e=>{
  const el = e.target;
  if(!el || !el.nodeType) return;
  if(_animAllow.has(el)) return;

  const now = performance.now();
  const prev = _animGuard.get(el) || 0;
  const gap = now - prev;

  /* 距上次开始不到 0.7 秒 → 判定为重播，立刻停掉 */
  if(prev && gap < 700){
    try{
      el.style.animation = 'none';
      /* 下一帧恢复 —— 否则后续正常的动画也播不出来 */
      requestAnimationFrame(()=>{ el.style.animation = ''; });
    }catch(err){}
    return;
  }
  _animGuard.set(el, now);
}, true);      /* 用捕获阶段，赶在浏览器绘制前拦下 */

/* 用户点击时，允许该元素（及其子元素）播动画 */
document.addEventListener('pointerdown', e=>{
  let n = e.target;
  let depth = 0;
  while(n && n.nodeType === 1 && depth < 3){
    _animAllow.add(n);
    setTimeout(()=>_animAllow.delete(n), 1500);
    n = n.parentElement;
    depth++;
  }
}, true);

/* ══════════ Teams / EC API ══════════ */
function apiTeamsStatus(){
  if(window.pywebview?.api?.teams_status) return window.pywebview.api.teams_status();
  return Promise.resolve({ok:false, ready:false, note:'当前模式不支持'});
}
function apiTeamsOpen(){
  if(window.pywebview?.api?.teams_open) return window.pywebview.api.teams_open();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}
function apiTeamsSync(rounds){
  if(window.pywebview?.api?.teams_sync)
    return window.pywebview.api.teams_sync(rounds == null ? 8 : rounds);
  return Promise.resolve({ok:false, reason:'当前模式不支持'});
}
/*  全部 Teams 消息（所有团队 / 所有频道）。
    Teams 整页用这个 —— 数据来自后端全量抓取的结果，
    每条都带 team / channel / date / kind 字段。 */
function apiTmAll(kw, date, channel, team){
  if(window.pywebview?.api?.tm_all)
    return window.pywebview.api.tm_all(kw||'', date||'',
                                       channel||'', team||'');
  /* 预览 / 网页模式：退回 ec_list，至少能用 */
  if(window.pywebview?.api?.ec_list)
    return window.pywebview.api.ec_list(kw||'', date||'');
  return Promise.resolve({ok:false, items:[], total:0});
}
/*  Teams 全量数据的概览（团队数 / 频道数 / 帖子数 / 抓取时间） */
function apiTmStats(){
  if(window.pywebview?.api?.tm_stats) return window.pywebview.api.tm_stats();
  return Promise.resolve({ok:false, nTeams:0, nChannels:0, nPosts:0});
}
/*  发起一次全量抓取（后台跑，用 apiTmCrawlStatus 轮询进度） */
function apiTmCrawl(scroll, deep){
  if(window.pywebview?.api?.tm_crawl) return window.pywebview.api.tm_crawl(scroll||10, !!deep);
  return Promise.resolve({ok:false, reason:'当前模式不支持'});
}
function apiTmCrawlStatus(){
  if(window.pywebview?.api?.tm_crawl_status)
    return window.pywebview.api.tm_crawl_status();
  return Promise.resolve({ok:true, running:false, message:''});
}
function apiTeamsDownload(){
  if(window.pywebview?.api?.teams_download_ec) return window.pywebview.api.teams_download_ec();
  return Promise.resolve({ok:false, reason:'当前模式不支持'});
}

/* ══════════ 通信 ══════════ */
window.updateData = json=>{
  try{
    DATA = typeof json==='string'?JSON.parse(json):json;
    /*  记住学校网址 —— 引导页用它预填输入框 */
    if(DATA.host) DEFAULT_HOST = DATA.host;
    render();
  }catch(e){ console.error('数据解析失败',e); }
};

/*  轮询：每 1 秒取一次状态，让倒计时与真实进度同步（不自己跑计时器） */
let pollTimer = null;
let pollBusy = false;
let warmTimer = null;
let tickTimer = null;
let digTimer = null;
function poll(){
  if(pollBusy) return;
  pollBusy = true;
  apiGetData()
    .then(d=>{ if(d) window.updateData(d); })
    .catch(()=>{})
    .finally(()=>{ pollBusy = false; });
}

/* 每秒只更新倒计时（不重建 DOM，不打扰滚动/折叠） */
function tick(){
  try{ tickCountdown(); }catch(e){}
}

/* 预热状态变化很少，单独低频轮询（每 30 秒一次） */
function pollWarm(){
  apiWarmStatus()
    .then(w=>{
      if(!w) return;
      WARM = w;
      /*  内部已经有「文字没变就不动 DOM」的判断 */
      renderWarmBadge();
    })
    .catch(()=>{});
}

document.addEventListener('DOMContentLoaded',()=>{
  /*  网页模式没有真实窗口可管 ——
      最小化/最大化/关闭这组藏起来，免得点了没反应。
      （判定在 syncWinbtnsVisibility 里，桥到了会自己纠正。） */
  syncWinbtnsVisibility();

  /*  首次运行：看有没有填过账号。
     没填过 → 弹出引导页（盖住主界面）；
     填过   → 什么都不做，直接进主界面。 */
  bindSetup();
  bootSetup();
  checkDbHealth();
  checkBrowserHealth();

  /* Tab 点击 */
  document.querySelectorAll('.tab').forEach(el=>{
    el.addEventListener('click', ()=> switchTab(+el.dataset.tab));
  });

  /* 左右滑动切换（共 5 页） */
  const MAXTAB = 4;
  const vp = $('#viewport');
  if(vp){
    let sx = 0, sy = 0, dragging = false;
    vp.addEventListener('touchstart', e=>{
      sx = e.touches[0].clientX; sy = e.touches[0].clientY; dragging = true;
    }, {passive:true});
    vp.addEventListener('touchmove', e=>{
      if(!dragging) return;
      const dx = e.touches[0].clientX - sx, dy = e.touches[0].clientY - sy;
      if(Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)*1.6){
        dragging = false;
        if(dx < 0 && curTab < MAXTAB) switchTab(curTab + 1);
        else if(dx > 0 && curTab > 0) switchTab(curTab - 1);
      }
    }, {passive:true});
    vp.addEventListener('touchend', ()=>{ dragging = false; }, {passive:true});

    /* 鼠标横向滚轮 / Shift+滚轮也可切换
       ★ 判据要「明确是横向意图」才切页：
         触控板两指斜着滑时 deltaX 也不小，判松了会让人在正常滚动时
         莫名其妙换页。所以要求横向量足够大、同时纵向量足够小。 */
    vp.addEventListener('wheel', e=>{
      if(Math.abs(e.deltaX) > 40 && Math.abs(e.deltaY) < 12){
        if(e.deltaX > 0 && curTab < MAXTAB) switchTab(curTab + 1);
        else if(e.deltaX < 0 && curTab > 0) switchTab(curTab - 1);
      }
    }, {passive:true});
  }

  /* 键盘左右方向键 */
  document.addEventListener('keydown', e=>{
    /*  教程开着时不切页 —— 用户是在看教程，
        底下的页签跟着翻会让人莫名其妙。 */
    if(document.getElementById('tour')?.classList.contains('open')) return;
    /* 输入框里按方向键不切页 */
    const t = e.target;
    if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
      || t.tagName === 'SELECT')) return;
    if(e.key === 'ArrowRight' && curTab < 4) switchTab(curTab + 1);
    else if(e.key === 'ArrowLeft' && curTab > 0) switchTab(curTab - 1);
  });

  /* 恢复上次所在页 */
  const saved = parseInt(localStorage.getItem('mb.tab') || '0', 10);
  switchTab(Number.isFinite(saved) ? saved : 0);

  poll();
  pollWarm();
  refreshDigestDot();
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1000);
  if(tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(tick, 1000);
  if(warmTimer) clearInterval(warmTimer);
  warmTimer = setInterval(pollWarm, 30000);
  if(digTimer) clearInterval(digTimer);
  /* 晚报状态变化很慢（一天一次），低频轮询就够 */
  digTimer = setInterval(refreshDigestDot, 120000);
});
window.addEventListener('pywebviewready', ()=>{
  poll(); pollWarm(); refreshDigestDot();
});

/* 页面重新可见时立即刷新一次（避免长时间最小化后数据陈旧） */
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){ poll(); pollWarm(); } });


/* ══════════════ 小浮窗（compact 形态）══════════════
   这是「小组件」这三个字的本意：浮在桌面上的一小块，
   扫一眼就知道下一节课是什么、还有多少事没做。

   ★ 它只读 DATA，不写 DATA。
   ★ 它只切 body 上的一个类，不重建任何 DOM ——
     所以来回切形态不会丢滚动位置或展开状态。
   ══════════════════════════════════════════════════════════════════ */

const SHAPE_KEY = 'mb.shape';
/*  小浮窗已删除：本地若还留着 compact，直接丢弃，一律进完整形态 */
let SHAPE = 'full';
try{ localStorage.removeItem(SHAPE_KEY); }catch(_){ }
/* 置顶状态：真正的开关在后端（窗口属性），
   这里只记「界面显示成什么样」，每次进设置都重新问一次。 */
let ON_TOP = false;

const isWidgetMode = () => hasBridge();
const miniEl = () => document.getElementById('mini');

/* ── 切形态：先改界面，再让后端调窗口大小 ──
   ★ 顺序有讲究：先给 body 加类（界面立刻变成小浮窗的样子），
     再让后端去 resize。反过来的话，窗口已经缩了、
     界面还是完整布局，会看到一瞬间的挤压变形。 */
async function setShape(shape, quiet){
  /*  ★ 0.5.2：小浮窗（compact）形态已删除 ★

      窗口只有「最大化（不是全屏）」与「最小化」两种状态。
      这个函数保留（调用点很多），但只做一件事：
      确保界面是完整形态，并清掉历史上留在本地的 compact 记录。

      ★ 不再调用后端 win_set('shape')：
        后端已改为只接受 full，调它只会拿到一个拒绝。 */
  if(SHAPE !== 'full'){
    SHAPE = 'full';
    try{ localStorage.removeItem(SHAPE_KEY); }catch(_){ }
  }
  document.body.classList.remove('compact');
  setMiniLabels();
  syncShapeSeg();
  return {ok: true, shape: 'full'};
}

/*  面板上「完整界面 / 小浮窗」那两格的选中态，跟着 SHAPE 走。
    单独抽一个函数是因为有三处要同步：切形态时、开机时、点「开机右上角」时。 */
function syncShapeSeg(){
  document.querySelectorAll('#setShape span').forEach(x=>
    x.classList.toggle('on', x.dataset.s === SHAPE));
}

/* 置顶开关：后端真的去改窗口的 TOPMOST 属性 */
async function setOnTop(on, quiet){
  const a = window.pywebview && window.pywebview.api;
  if(!(a && typeof a.win_set === 'function')){
    /*  网页版 / 后端没起来：这一项在浏览器里本来就做不到。
        不要弹「不能改」的错 —— 而是**把这一项藏掉**，
        免得用户反复点、反复失败。
        （踩过：网页版点置顶，弹「置顶没能改成功」，
          但那根本不是用户的问题，是环境不支持。） */
    hideWidgetOnlyRows();
    if(!quiet) return false;
    return false;
  }
  try{
    const r = await a.win_set('on_top', !!on);
    const ok = !!(r && r.ok);
    if(ok){
      ON_TOP = !!on;
      localStorage.setItem('mb.ontop', on ? '1' : '0');
      syncTopBtn();
    }else if(!quiet){
      toast('置顶没能改成功：' + ((r && r.error) || '未知原因'), 'err', '');
    }
    return ok;
  }catch(e){
    if(!quiet) toast('置顶没能改成功：' + e, 'err', '');
    return false;
  }
}

/*  ── 只在桌面版有意义的几项，在网页版直接藏掉 ──
    包括：形态、置顶、开机右上角、开机自动更新。
    （小浮窗在浏览器里没窗口可缩，形态改了也没用。） */
function hideWidgetOnlyRows(){
  ['setShapeRow', 'setTopRow', 'setPinRow', 'setMiniRow'].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.hidden = true;
  });
  /*  老 id 兼容（setShape 那一行的容器当时没起 id） */
  const sc = document.getElementById('setShape');
  if(sc && sc.closest('.setrow')) sc.closest('.setrow').hidden = true;
}

/*  ★★★ 0.5.2 修：把上面那几行**恢复显示**。

    【踩过的坑（用户反馈「找不到关闭置顶按键」）】
      bootSettingsMini() 在 DOMContentLoaded 时就跑，而 pywebview 的桥
      （window.pywebview.api）是**页面加载完成之后**才注入的。
      于是那句判断：

          if(!(a && typeof a.win_set === 'function')) hideWidgetOnlyRows();

      在桌面版启动的**那一刻**也成立 —— 桥还没来，看起来就像网页版，
      直接把「固定在其他窗口上层」那几行永久藏掉了。
      而 bootSettingsMini 带 _done 守卫，只跑一次，
      等桥真的注入时已经晚了，再也不会恢复。

      后果：**桌面版用户根本看不到置顶开关**，也看不到「开机以小浮窗」
      与「窗口形态」那两行。

    【修法】
      · 判断改为「等桥就绪再定」（见 bootSettingsMini 里的 waitBridge）
      · 同时提供这个恢复函数，发现桥到了就把行显示回来（幂等）
 */
function showWidgetOnlyRows(){
  ['setShapeRow', 'setTopRow', 'setPinRow', 'setMiniRow'].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.hidden = false;
  });
  const sc = document.getElementById('setShape');
  if(sc && sc.closest('.setrow')) sc.closest('.setrow').hidden = false;
}

/*  等桥就绪（最多 3 秒），到了调 onReady。
    桥注入得比 DOMContentLoaded 晚，所以不能当场判断。 */
function waitBridge(onReady, tries){
  const n = tries == null ? 30 : tries;
  const a = window.pywebview && window.pywebview.api;
  if(a && typeof a.win_set === 'function'){ onReady(true); return; }
  if(n <= 0){ onReady(false); return; }
  setTimeout(()=> waitBridge(onReady, n - 1), 100);
}

function syncTopBtn(){
  const b = document.getElementById('mnTop');
  if(b) b.classList.toggle('on', ON_TOP);
  const sw = document.getElementById('setTop');
  if(sw) sw.classList.toggle('on', ON_TOP);
}

/* ══════════ 小浮窗内容 ══════════
   只做三件事：下一节课、三个数字、一行提示。
   刻意**不**做「最近成绩」「待办列表」—— 那些是完整界面的事，
   小窗塞满了就不叫小窗了。 */
function renderMini(){
  /*  0.5.2：小浮窗面板已从 index.html 删除，这里只需安全返回。
      保留函数名是因为 poll 里还有调用点。 */
  if(!miniEl()) return;

  const el = miniEl();
  if(!el) return;

  const lessons = ((DATA || {}).schedule || {}).lessons || [];

  /* ── ① 下一节课 ── */
  const host = document.getElementById('mnNext');
  if(host){
    const nx = findNextLesson(lessons);
    /*  小浮窗里的这几行**每秒都在变**（倒计时），
        所以不能整块重建 —— 会看到文字在闪。
        拆成「结构」和「会变的那个数」两部分：
        结构只在换课/换状态时重建，数字每秒就地更新。 */
    const sig = nx
      ? [nx.lesson.subject, nx.lesson.start, nx.lesson.room,
         nx.lesson.teacher, nx.running].join('|')
      : '__none__';

    if(host.dataset.sig !== sig){
      host.dataset.sig = sig;

      if(!nx){
        host.className = 'mn-next';
        host.style.removeProperty('--mc');
        host.innerHTML = `<div class="mn-none">
          <div class="mn-ic">${icon('i-sunrise','xxl')}</div>
          <div class="mn-t">暂时没有课</div>
          <div class="mn-d">课表里还没有安排，或者课都上完了</div>
        </div>`;
      }else{
        const l = nx.lesson;
        const endM = minsUntil(l.end);
        const running = !!nx.running;
        const soon = !running && nx.mins != null && nx.mins <= 30
                     && l.day === todayISO();
        const col = running ? 'var(--green)'
                  : soon ? 'var(--orange)' : 'var(--blue)';
        const tag = running ? '<span class="mn-tag now">进行中</span>'
                  : soon ? '<span class="mn-tag soon">马上开始</span>'
                  : '<span class="mn-tag next">下一节</span>';

        /* 自编课有 [上操] 这种前缀，拆出来当标签使 */
        let title = l.subject || '(未命名)';
        let flag = '';
        const fm = /^\[([^\]]+)\]\s*(.*)$/.exec(title);
        if(fm){ flag = fm[1]; title = fm[2] || fm[1]; }

        const meta = [];
        if(l.room) meta.push(`<span>${esc(l.room)}</span>`);
        if(l.teacher) meta.push(`<span>${esc(l.teacher)}</span>`);
        if(flag) meta.unshift(`<span><b>${esc(flag)}</b></span>`);
        if(l.end && !l.selfStudy){
          meta.push(`<span><b>${esc(hm(l.start))}–${esc(hm(l.end))}</b></span>`);
        }else{
          meta.push(`<span><b>${esc(hm(l.start))}</b></span>`);
        }

        host.className = 'mn-next' + (running ? ' now' : '');
        host.style.setProperty('--mc', col);
        host.innerHTML = `
          <div class="mn-lab">${running ? '现在在上' : '接下来'}${tag}</div>
          <div class="mn-subj">${esc(title)}</div>
          <div class="mn-meta">${meta.join('')}</div>
          <div class="mn-when" id="mnWhen"></div>`;
      }
    }

    /* 会变的那个数：每秒（以及每次数据更新）重算 */
    const whenEl = document.getElementById('mnWhen');
    if(whenEl && nx){
      const l = nx.lesson;
      const endM = minsUntil(l.end);
      let txt;
      if(nx.running){
        txt = (endM != null && endM > 0)
          ? `${countdownText(endM)}下课` : '正在进行';
      }else{
        txt = whenText(l, nx.mins);
      }
      if(whenEl.dataset.v !== txt){
        whenEl.dataset.v = txt;
        whenEl.innerHTML = `${icon('i-clock','sm')}${esc(txt)}`;
      }
    }
  }

  /* ── ② 三个数字 ──
      挑最需要「一眼看到」的三个：
        还有多少事没做 / 有几件已经过期 / GPA。 */
  const st = document.getElementById('mnStats');
  if(st){
    const tasks = ((DATA || {}).tasks || []).map(liveTask);
    const todo = tasks.filter(x => !x.completed);
    const overdue = todo.filter(x =>
      x.days_left != null && x.days_left < 0).length;
    const s = (DATA || {}).summary || {};

    /*  GPA 口径必须和完整界面一致 ——
        官方值优先，没有就用估算，都没有显示破折号。 */
    let gpaN = '—', gpaC = 'var(--dim)';
    const off = s.gpa_official;
    if(off && off.value != null){
      gpaN = Number(off.value).toFixed(2); gpaC = 'var(--green)';
    }else if(s.gpa_estimated != null){
      gpaN = Number(s.gpa_estimated).toFixed(2); gpaC = 'var(--blue)';
    }

    const cells = [
      {n: todo.length, l: '待完成', c: overdue ? 'var(--blue)' : 'var(--blue)',
       tab: 0},
      {n: overdue, l: '已过期',
       c: overdue ? 'var(--red)' : 'var(--dim)', tab: 0},
      {n: gpaN, l: 'GPA', c: gpaC, tab: 2},
    ];

    const csig = JSON.stringify(cells.map(x => [x.n, x.l, x.c]));
    if(st.dataset.sig !== csig){
      st.dataset.sig = csig;
      st.innerHTML = cells.map(x => `
        <div class="mn-cell" data-tab="${x.tab}" title="打开完整界面看详情">
          <div class="n" style="color:${x.c}">${esc(String(x.n))}</div>
          <div class="l">${esc(x.l)}</div>
        </div>`).join('');
      /* 点数字 → 展开完整界面并跳到那一页 */
      st.querySelectorAll('.mn-cell').forEach(c=>{
        c.addEventListener('click', async ()=>{
          const t = +c.dataset.tab;
          await setShape('full', true);
          switchTab(t, true);
        });
      });
    }
  }

  /* ── ③ 底部提示 ──
      只在「数据旧了」或「出错了」时出现。
      正常时完全不占位 —— 小窗口里每个像素都金贵。 */
  const mmsg = document.getElementById('mnMsg');
  if(mmsg){
    let txt = '', kind = '';
    const d = DATA || {};
    if(d.status === 'error'){
      txt = d.message || '上次抓取失败了'; kind = 'err';
    }else if(d.status === 'need_login'){
      txt = d.message || '需要重新登录'; kind = 'err';
    }else if(d.status === 'running'){
      txt = '正在更新……'; kind = 'stale';
    }else{
      const w = WARM || {};
      const age = w.cache_age;
      if(age != null && age > 7200){
        txt = `${Math.floor(age/3600)} 小时前的数据`;
        kind = 'stale';
      }
    }
    const show = !!txt;
    mmsg.className = show ? `mn-msg show ${kind}` : 'mn-msg';
    if(show && mmsg.textContent !== txt) mmsg.textContent = txt;
  }
}

/* ── 小浮窗里的按钮 ── */
function bindMini(){
  const rf = document.getElementById('mnRefresh');
  if(rf && !rf.dataset.bound){
    rf.dataset.bound = '1';
    rf.addEventListener('click', async ()=>{
      rf.classList.add('spin');
      try{ await apiRefresh(); }catch(e){}
      setTimeout(()=>rf.classList.remove('spin'), 1200);
    });
  }

  const tp = document.getElementById('mnTop');
  if(tp && !tp.dataset.bound){
    tp.dataset.bound = '1';
    tp.addEventListener('click', ()=> setOnTop(!ON_TOP));
  }

  const fu = document.getElementById('mnFull');
  if(fu && !fu.dataset.bound){
    fu.dataset.bound = '1';
    fu.addEventListener('click', ()=> setShape('full'));
  }
}

/* ── 标题栏上的「收成小浮窗」 ── */
function bindMiniToggle(){
  const b = document.getElementById('btnMini');
  if(!b) return;   /* 0.5.2：小浮窗按钮已删除 */
  if(b && !b.dataset.bound){
    b.dataset.bound = '1';
    b.addEventListener('click', ()=> setShape('compact'));
  }
}

/* ══════════ 启动时的形态恢复 ══════════
   两种来源要分清：
     · 网页版         → 只有 localStorage（没有窗口可调）
     · 桌面版         → **后端说了算**。窗口大小是后端建的，
                        前端 localStorage 可能和它对不上
                        （比如用户改了 data/window.json，
                          或者打包时写死了 compact）。
   所以桌面版要问一次后端，以后端为准。 */
async function bootShape(){
  const a = window.pywebview && window.pywebview.api;

  /* 先按本地记的上（不闪默认样式） */
  document.body.classList.toggle('compact', SHAPE === 'compact');
  bindMini();

  if(!(a && typeof a.win_cfg === 'function')){
    /*  网页版：没有后端可问，但设置面板里的控件还是要接上 */
    bootSettingsMini({});
    return;
  }

  try{
    const r = await a.win_cfg();
    if(r && r.cfg && r.cfg.shape){
      const s = (r.cfg.shape === 'compact') ? 'compact' : 'full';
      if(s !== SHAPE){
        /*  ★ 0.5.1：不一致时走 setShape（quiet），别只改类名。
            只改类的话窗口尺寸还是旧形态的 ——
            踩过：localStorage 记着 compact、后端记着 full，
            开机就是「小浮窗界面 + 大窗口」，留白一半、
            底部按钮被顶到任务栏以下。setShape 会把窗口
            尺寸一起摆正。 */
        await setShape(s, true);
      }else{
        document.body.classList.toggle('compact', s === 'compact');
      }
    }
    /* 置顶：也以后端的为准 */
    if(r && r.cfg && typeof r.cfg.on_top === 'boolean'){
      ON_TOP = !!r.cfg.on_top;
    }
    syncTopBtn();
    bootSettingsMini(r && r.cfg);
  }catch(e){
    bootSettingsMini({});
  }
}

/* ══════════ 设置面板里那几项的接法 ══════════
   ★★ 这里踩过一个坑，说明白免得再犯 ——

     `app.js` 在 index.html 里是**紧跟首次引导页**引入的，
     而设置面板在文件**更靠后**的位置。
     所以脚本刚解析到这一行的时候，
     `#setShape` / `#setTop` / `#setPin` **根本还不存在** ——
     `querySelectorAll` 返回空列表，监听一个都没贴上。
     表现就是：面板打开着，但那三个控件**点不动**。

     （同样的问题在配色那边也有过一次，
       见 `bootThemeLater` 的说明。凡是「要操作设置面板里的元素」
       的初始化，都得等 DOM 解析完。）

   → 所以这里拆成两步：
       ① bootShape() 只管**数据**（问后端要形态/置顶）
       ② bootSettingsMini() 只管**接控件**，等 DOM 好了再跑
*/
function bootSettingsMini(cfg){
  if(bootSettingsMini._done) return;
  if(!document.getElementById('setShape')) return;   /* 面板还没来 */
  bootSettingsMini._done = true;
  bindSettingsMini(cfg || {});

  /*  ★★★ 等桥就绪再判断显隐（修「桌面版看不到置顶开关」）
      桥注入得比这里晚，当场判断会把桌面版误判成网页版，
      然后就把那几行永久藏掉了。 */
  waitBridge((isDesktop)=>{
    if(isDesktop){
      showWidgetOnlyRows();     /* 桌面版：确保显示（幂等） */
    }else{
      hideWidgetOnlyRows();     /* 真网页版：藏掉 */
    }
  });
}
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', ()=> bootSettingsMini());
}else{
  setTimeout(()=> bootSettingsMini(), 0);
}
/*  双保险：DOM 早过了也不会漏（同 bindSettings 的做法） */
setTimeout(()=> bootSettingsMini(), 800);


/* ══════════ 设置面板里新增的三项 ══════════ */
function bindSettingsMini(cfg){
  cfg = cfg || {};

  /* 形态：两档 */
  document.querySelectorAll('#setShape span').forEach(el=>{
    if(el.dataset.mbound) return;
    el.dataset.mbound = '1';
    el.classList.toggle('on', el.dataset.s === SHAPE);
    el.addEventListener('click', async e=>{
      e.stopPropagation();
      document.querySelectorAll('#setShape span').forEach(x=>
        x.classList.toggle('on', x === el));
      await setShape(el.dataset.s);
      setMiniLabels();
    });
  });

  /* 置顶 */
  const tp = document.getElementById('setTop');
  if(tp && !tp.dataset.mbound){
    tp.dataset.mbound = '1';
    tp.classList.toggle('on', ON_TOP);
    tp.addEventListener('click', async e=>{
      e.stopPropagation();
      tp.disabled = true;
      const want = !ON_TOP;
      const ok = await setOnTop(want, true);
      if(ok){
        tp.classList.toggle('on', want);
        toast(want ? '已固定在其他窗口上层' : '已取消置顶', 'ok', '');
      }else{
        toast('置顶没能改成功', 'err', '');
      }
      tp.disabled = false;
    });
  }

  /* 开机摆到右上角 */
  const pr = document.getElementById('setPin');
  if(pr && !pr.dataset.mbound){
    pr.dataset.mbound = '1';
    pr.classList.toggle('on', !!cfg.pin_right);
    pr.addEventListener('click', async e=>{
      e.stopPropagation();
      pr.disabled = true;
      const want = !pr.classList.contains('on');
      const a = window.pywebview && window.pywebview.api;
      try{
        const r = (a && typeof a.win_set === 'function')
          ? await a.win_set('pin_right', want) : null;
        if(r && r.ok){
          pr.classList.toggle('on', want);
          /*  只开「贴右上角」还不够 ——
              要真的在开机时是那个样子，形态也得是小浮窗。
              这里用 setShape() 而不是自己改 SHAPE 和类名，
              免得两处状态不一致（踩过：标签和实际对不上）。 */
          if(want && SHAPE !== 'compact'){
            await setShape('compact', true);
          }
          toast(want ? '开机会以小浮窗出现在右上角' : '已关闭', 'ok', '');
        }else{
          toast('没能改成功：' + ((r && r.error) || '未知原因'), 'err', '');
        }
      }catch(err){
        toast('没能改成功：' + err, 'err', '');
      }
      pr.disabled = false;
      setMiniLabels();
      syncShapeSeg();
    });
  }

  setMiniLabels();
  syncShapeSeg();
}

function setMiniLabels(){
  const v = document.getElementById('setValShape');
  if(v) v.textContent = (SHAPE === 'compact') ? '小浮窗' : '完整界面';
}

/* ══════════ 和主流程的关系 ══════════
   ★ 本来想「包一层 render()」—— 在它跑完之后跟着更新小浮窗。
     实测这条路走不通，记下原因免得再试：

       app.js 里的 ``render()`` 是**函数声明**，
       别处调它写的是裸名字 ``render()``，会直接解析到那个声明上，
       改 ``window.render`` 根本不影响它们。

   → 改用最简单的办法：小浮窗开着的时候，**每秒重算一次**
     （``startMiniTick``）。数据变了最多 1 秒后就反映出来，
     而 ``renderMini`` 自带「值没变就不碰 DOM」的签名判断，
     所以每秒跑一次的开销只是一次字符串比较。
*/

/* 小浮窗里那个倒计时也要每秒走 ——
   它不在 #page-sch 里，所以 tickCountdown() 管不到它。 */
let miniTickTimer = null;
function startMiniTick(){
  if(miniTickTimer) return;
  miniTickTimer = setInterval(()=>{
    if(document.body.classList.contains('compact')) renderMini();
  }, 1000);
}


/* ══════════ 小浮窗：启动 ══════════ */
bindMiniToggle();
bootShape();
startMiniTick();


/* ══════════════ 配色预设 ══════════════
   一套预设 = 一组 CSS 变量的值。换主题就是一次性写进去。

   ★ 为什么不做成 `.theme-x` 类：
     切主题要和「透明度」「毛玻璃」叠加（那两项也在改变量）。
     用类名的话两处都在改同一个变量，谁赢取决于选择器权重，
     很难推理。统一走 JS 写内联变量 —— 只有一条路径，不会打架。

   ★ 为什么每个预设要给九个值：
     只换底色的话，玻璃卡片还是米白的、阴影还是暖褐的、
     背景光斑还是原来的暖黄 —— 换汤不换药，一眼看出没配套。 */
const THEMES = {
  /*  ── 浅绿系（用户点名要的）──
      清爽的青草色，配一点点黄绿光斑 */
  green: {
    name: '浅绿',
    bg:'#eef5ec', bg2:'#e4efe2', tint:'250,255,250',
    shadow:'86,110,84', line:'196,214,192',
    glow1:'#f7fdf5', glow2:'#e8f4e4', glow3:'#dfeedd',
    bgRGB:'238,245,236', fade:'rgba(238,245,236,.95)',
    scrim:'rgba(48,58,46,.26)',
  },
  /*  ── 米白暖色（原本的默认）── */
  warm: {
    name: '米白',
    bg:'#f5f1e8', bg2:'#efe9dc', tint:'255,253,248',
    shadow:'120,105,80', line:'214,202,179',
    glow1:'#fefbf3', glow2:'#f8f0e3', glow3:'#efe8d9',
    bgRGB:'245,241,232', fade:'rgba(245,241,232,.95)',
    scrim:'rgba(61,56,48,.26)',
  },
  /*  ── 冷灰（安静、适合长时间看）── */
  gray: {
    name: '冷灰',
    bg:'#f2f3f5', bg2:'#e8eaed', tint:'253,254,255',
    shadow:'92,99,110', line:'206,211,218',
    glow1:'#fbfcfd', glow2:'#eef1f4', glow3:'#e6e9ed',
    bgRGB:'242,243,245', fade:'rgba(242,243,245,.95)',
    scrim:'rgba(46,50,56,.26)',
  },
  /*  ── 浅蓝 ── */
  blue: {
    name: '浅蓝',
    bg:'#eef3f9', bg2:'#e3ecf6', tint:'250,253,255',
    shadow:'80,100,126', line:'194,208,226',
    glow1:'#f8fbff', glow2:'#e9f1fa', glow3:'#dee9f6',
    bgRGB:'238,243,249', fade:'rgba(238,243,249,.95)',
    scrim:'rgba(44,54,68,.26)',
  },
  /*  ── 藕粉 ── */
  pink: {
    name: '藕粉',
    bg:'#f8f0f2', bg2:'#f2e6ea', tint:'255,252,253',
    shadow:'124,94,104', line:'224,202,210',
    glow1:'#fffafb', glow2:'#faeef1', glow3:'#f3e4e9',
    bgRGB:'248,240,242', fade:'rgba(248,240,242,.95)',
    scrim:'rgba(64,50,54,.26)',
  },
  /*  ── 深色（护眼，晚上用）──
      深色下玻璃要「往亮里透」才对：tint 用偏白的低 alpha，
      阴影改用纯黑（暖褐在黑底上看不见）*/
  dark: {
    name: '深色',
    bg:'#25272b', bg2:'#1f2125', tint:'58,62,68',
    shadow:'0,0,0', line:'62,66,73',
    glow1:'#2d3036', glow2:'#22242a', glow3:'#1c1e22',
    bgRGB:'37,39,43', fade:'rgba(37,39,43,.95)',
    scrim:'rgba(0,0,0,.5)',
    dark: true,
  },
};

/* 深色主题要额外改一批「跟底色对比」的变量 ——
   文字、卡片、标签…如果只换底色，深底上会是黑字，读不了。 */
const DARK_VARS = {
  '--text':'#e6e4e0', '--muted':'#a8a49c', '--dim':'#847f78',
  '--card':'#2e3136', '--card2':'#33363c',
  '--line':'#3e4247', '--line2':'#4a4e54',
  '--glass':'rgba(58,62,68,.72)',
  '--glass-2':'rgba(58,62,68,.55)',
  '--glass-3':'rgba(58,62,68,.88)',
  '--glassTop':'inset 0 1px 0 rgba(255,255,255,.09)',
  '--glassEdge':'rgba(255,255,255,.14)',
};
/* 亮色主题要把这些**显式还原** ——
   否则从深色切回亮色时，上面那批还留在 :root 上。 */
const LIGHT_VARS = {
  '--text':'#3d3830', '--muted':'#8a8171', '--dim':'#a89e8b',
  '--card':'#fffdf8', '--card2':'#f7f2e7',
  '--line':'#e2d9c7', '--line2':'#d6cab3',
  '--glass':'rgba(255,253,248,.72)',
  '--glass-2':'rgba(255,253,248,.55)',
  '--glass-3':'rgba(255,253,248,.86)',
  '--glassTop':'inset 0 1px 0 rgba(255,255,255,.75)',
  '--glassEdge':'rgba(255,255,255,.62)',
};

const THEME_KEY = 'mb.theme';
const BGI_KEY   = 'mb.bgimg';      /* 背景图（dataURL，存 localStorage） */
const BGA_KEY   = 'mb.bgalpha';

let THEME_NAME = localStorage.getItem(THEME_KEY) || 'warm';
let CUSTOM_BG = localStorage.getItem('mb.bg') || '';
/*  自定义字色。空 = 没设过（跟着预设 / 自动挑） */
let CUSTOM_TEXT = localStorage.getItem('mb.fg') || '';
/*  最近一次算出来的那套字色（正文/次要/三级），对比度提示要用 */
let TEXT_LOCK = null;
let BG_IMG = '';

/* 把一套配色写进 :root */
function applyTheme(name){
  const t = THEMES[name] || THEMES.warm;
  const rt = document.documentElement;

  /*  ① 先还原「对比色」那批 —— 深色切回亮色时要用 */
  const base = t.dark ? DARK_VARS : LIGHT_VARS;
  for(const k in base) rt.style.setProperty(k, base[k]);

  /*  ② 再写这套预设自己的九个值 */
  rt.style.setProperty('--bgColor',  t.bg);
  rt.style.setProperty('--bgColor2', t.bg2);
  rt.style.setProperty('--bgRGB',    t.bgRGB);
  rt.style.setProperty('--tintRGB',  t.tint);
  rt.style.setProperty('--shadowRGB', t.shadow);
  rt.style.setProperty('--lineRGB',  t.line);
  rt.style.setProperty('--glow1',    t.glow1);
  rt.style.setProperty('--glow2',    t.glow2);
  rt.style.setProperty('--glow3',    t.glow3);
  rt.style.setProperty('--fadeTop',  t.fade);
  rt.style.setProperty('--scrim',    t.scrim);

  /*  ③ 自定义底色盖在预设之上（如果设过）
         —— 它会连带处理字色（自动挑 + 用户自定） */
  if(CUSTOM_BG) applyCustomBg(CUSTOM_BG, true);

  /*  ④ 用户自己定过字色 → 盖掉预设那套
         （放在自定义底色之后：用户明确指定的优先级最高） */
  if(CUSTOM_TEXT) applyCustomText(CUSTOM_TEXT, true);

  /*  ⑤ 玻璃那三层要按新底色重算 ——
        applyLook() 里写的是「米白」的固定写法，
        换主题之后得用新 tint 重写一遍，否则玻璃还是米白的。 */
  if(typeof applyLook === 'function' && LOOK) applyLook(LOOK);

  /*  ⑥ 深色主题下高光要淡一点（见 lqSyncDark 的说明） */
  if(typeof lqSyncDark === 'function'){
    try{ lqSyncDark(); }catch(e){}
  }
}

/* 从 '#rrggbb' 算出「亮一点的同色系」，返回**十进制**的 'R,G,B'
   （不是十六进制 —— 那个格式直接拼进 rgba() 是无效颜色） */
function lighten(hex, k){
  const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || '');
  if(!m) return null;
  /*  ★ parseInt(v, 16) —— 忘了写 16 会按十进制解析，
         "d9" 会变成 NaN，算出来的颜色整个是坏的 */
  const up = v => Math.min(255,
    Math.round(parseInt(v, 16) + (255 - parseInt(v, 16)) * k));
  return [up(m[1]), up(m[2]), up(m[3])].join(',');
}

/* 相对亮度（WCAG 那套）—— 用来判断「这个底色该配深字还是浅字」 */
function hexLum(hex){
  const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || '');
  if(!m) return null;
  const [r, g, b] = [m[1], m[2], m[3]].map(v=>{
    const c = parseInt(v, 16) / 255;
    return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4);
  });
  return 0.2126*r + 0.7152*g + 0.0722*b;
}

/* 两个颜色的对比度（1~21）。WCAG 正文要求 ≥4.5 */
function contrastOf(a, b){
  const la = hexLum(a), lb = hexLum(b);
  if(la == null || lb == null) return null;
  const hi = Math.max(la, lb), lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/*  自动挑一个「看得清」的字色。
    ★ 用户自己改底色之后最容易出的事就是字看不清 ——
      浅底配白字、深底配黑字，都是几分钟看不出问题、
      真用起来眼睛疼的那种。所以改底色时顺手算一遍对比度，
      不够就自动换一套字色，并且告诉他「自动调过了」。 */
function autoTextFor(bg, dark){
  /*  深色主题配浅字，亮色主题配深字 —— 但**还要看对比度**，
      深色主题下用户挑了个亮黄底色，就不能再用浅字了。 */
  const light = { text:'#e6e4e0', muted:'#a8a49c', dim:'#847f78' };
  const darkV =  { text:'#3d3830', muted:'#8a8171', dim:'#a89e8b' };
  const cand = dark ? [light, darkV] : [darkV, light];
  const pick = cand.find(set=>{
    const c = contrastOf(bg, set.text);
    return c != null && c >= 4.5;
  });
  return pick || cand[0];
}

/*  对比度提示（设置面板「当前对比度」那一行）。
    字色 / 底色任一变都调它；算不出（没底色没字色）就自己收起来。
    底色优先用自定义，其次当前预设；字色优先自定义，其次自动挑的那套。 */
function updateContrastHint(){
  const row = document.getElementById('setCtrRow');
  const cv = document.getElementById('setCtrVal');
  if(!row || !cv) return;
  const bg = CUSTOM_BG || (THEMES[THEME_NAME] || {}).bg || '';
  const fg = CUSTOM_TEXT || (TEXT_LOCK || {}).text
           || getComputedStyle(document.documentElement)
                .getPropertyValue('--text').trim();
  const cr = (bg && fg) ? contrastOf(bg, fg) : null;
  if(cr == null){
    row.classList.remove('show');
    return;
  }
  row.classList.add('show');
  const ok = cr >= 4.5;
  cv.textContent = cr.toFixed(1) + (ok ? '' : '（可能看不清）');
  cv.style.color = ok ? 'var(--green)' : 'var(--orange)';
}

function applyCustomBg(hex, quiet){
  CUSTOM_BG = hex || '';
  const rt = document.documentElement;
  if(!CUSTOM_BG){
    rt.style.removeProperty('--bgColor');
    rt.style.removeProperty('--bgRGB');
    rt.style.removeProperty('--tintRGB');
    rt.style.removeProperty('--fadeTop');
    rt.style.removeProperty('--text');
    rt.style.removeProperty('--muted');
    rt.style.removeProperty('--dim');
    /*  去掉覆盖 → 回到预设自己的值 */
    applyTheme(THEME_NAME);
    const tx0 = document.getElementById('setTextTx');
    if(tx0) tx0.textContent = '跟着预设走';
    updateContrastHint();      /* 底色清空 → 报预设组合的对比度 */
    return;
  }
  localStorage.setItem('mb.bg', CUSTOM_BG);
  rt.style.setProperty('--bgColor', CUSTOM_BG);

  const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(CUSTOM_BG);
  if(m){
    /*  ★ 必须转成**十进制**再拼 ——
          CSS 变量里 --bgRGB 的格式是「245,241,232」（十进制），
          而颜色选择器给的是十六进制字符串（"d9e8f5"）。
          直接把十六进制塞进去会得到
             rgba(d9,e8,f5,.95)
          这是**无效颜色**，浏览器会整条丢掉 ——
          症状是「视口上下那两条渐隐带不见了」，
          不报错、不提示，很难查。
          （踩过一次：值打印出来才看出来是 d9 不是 217。） */
    const dec = [m[1], m[2], m[3]].map(v => parseInt(v, 16));
    const arr = dec.join(',');
    rt.style.setProperty('--bgRGB', arr);
    /*  玻璃底：底色往白里提一点 —— 玻璃总比底色亮，才像玻璃 */
    const tt = lighten(CUSTOM_BG, 0.72);
    if(tt) rt.style.setProperty('--tintRGB', tt);
    /*  边缘渐隐要跟底色一致，否则会看出两条色带 */
    rt.style.setProperty('--fadeTop', `rgba(${arr},.95)`);

    /*  ── 字色：用户没自己定的话，按底色的明暗自动挑一套 ──
         见 autoTextFor() 的说明：改底色最容易把字弄成看不清，
         所以这里顺手算一遍对比度。 */
    if(!CUSTOM_TEXT){
      const isDark = !!(THEMES[THEME_NAME] || {}).dark;
      const set = autoTextFor(CUSTOM_BG, isDark);
      TEXT_LOCK = set;
      rt.style.setProperty('--text', set.text);
      rt.style.setProperty('--muted', set.muted);
      rt.style.setProperty('--dim', set.dim);
      const tx = document.getElementById('setTextTx');
      if(tx) tx.textContent = '已按底色自动选（' + set.text.toUpperCase() + '）';
    }
    /*  对比度兜底：不管字色是不是用户定的，都报一次数。
        低于 4.5 就提醒 —— 但不强行改（用户可能有他的理由）。 */
    updateContrastHint();
  }
  if(!quiet) toast('背景颜色已更新', 'ok', '');
}

/* ── 自定义字色 ──
   三种状态要分清楚：
     · 没设过    → CUSTOM_TEXT 空，跟着预设/自动走
     · 设过      → 用用户的值
     · 「自动」  → 把 CUSTOM_TEXT 清掉，让 autoTextFor 重新挑 */
function applyCustomText(hex, quiet){
  const rt = document.documentElement;
  CUSTOM_TEXT = hex || '';
  if(!CUSTOM_TEXT){
    localStorage.removeItem('mb.fg');
    rt.style.removeProperty('--text');
    rt.style.removeProperty('--muted');
    rt.style.removeProperty('--dim');
    TEXT_LOCK = null;
    /*  回到「跟着底色自动挑」——拿当前底色再算一遍 */
    if(CUSTOM_BG){
      const isDark = !!(THEMES[THEME_NAME] || {}).dark;
      const set = autoTextFor(CUSTOM_BG, isDark);
      TEXT_LOCK = set;
      rt.style.setProperty('--text', set.text);
      rt.style.setProperty('--muted', set.muted);
      rt.style.setProperty('--dim', set.dim);
    }
    const tx = document.getElementById('setTextTx');
    if(tx) tx.textContent = '跟着预设走';
    updateContrastHint();      /* 字色回到自动 → 报新组合的对比度 */
    if(quiet !== true) toast('字色已跟着底色', 'ok', '');
    return;
  }
  localStorage.setItem('mb.fg', CUSTOM_TEXT);
  /*  只改「正文色」，次要色按同色系推 ——
      只改一个 --text 的话，标签、说明文字还是旧色，
      深浅底之间切换会显得「半新半旧」。 */
  const set = textSetFrom(CUSTOM_TEXT);
  TEXT_LOCK = set;
  rt.style.setProperty('--text', set.text);
  rt.style.setProperty('--muted', set.muted);
  rt.style.setProperty('--dim', set.dim);

  /*  顺手报一下对比度 —— 用户挑了看不清的组合，要当场知道 */
  updateContrastHint();
  if(quiet !== true) toast('字体颜色已更新', 'ok', '');
}

/* 从一个「正文色」推出配套的次要色 ——
   正文往灰里调一点 = 次要色，再淡一点 = 三级色。
   这样三档颜色的色相一致，不会「正文是蓝的、说明是灰的」那么乱。 */
function textSetFrom(hex){
  const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || '');
  if(!m) return { text:hex, muted:hex, dim:hex };
  const v = [m[1], m[2], m[3]].map(x=>parseInt(x, 16));
  const L = hexLum(hex);
  /*  往哪个方向调：亮字（在深底上）要往暗里调才「次要」，
      暗字（在亮底上）要往亮里调。 */
  const dir = (L != null && L < 0.4) ? -1 : 1;
  const mix = (a, k)=>{
    /*  朝中灰（128）靠 —— 灰色在任何底色上都比纯色更像「次要」 */
    const t = 128;
    return Math.round(a + (t - a) * k);
  };
  const s1 = v.map(x=>mix(x, 0.42));
  const s2 = v.map(x=>mix(x, 0.62));
  const hx = a => '#' + a.map(x=>x.toString(16).padStart(2,'0')).join('');
  return { text:hex, muted:hx(s1), dim:hx(s2), dir };
}

/* ── 背景图片 ──
   存成 dataURL 放 localStorage（不上传、不依赖网络）。
   图太大会撑爆 localStorage（一般 5MB 配额），
   所以先缩到最长边 1600 再存。

   渲染靠 CSS 的 body::before 那一层（见 app.css），
   这里只负责把 url 和浓度写到变量上。 */
function applyBgImage(dataUrl){
  const rt = document.documentElement;
  if(!dataUrl){
    rt.style.setProperty('--bgImage', 'none');
    BG_IMG = '';
    return;
  }
  BG_IMG = dataUrl;
  /*  变量值里带引号 —— 路径里的括号、空格才不会截断 url() */
  rt.style.setProperty('--bgImage', 'url("' + dataUrl + '")');
  applyBgAlpha(localStorage.getItem(BGA_KEY) || 100);
}

function applyBgAlpha(v){
  const a = Math.max(0.1, Math.min(1, (+v || 100) / 100));
  document.documentElement.style.setProperty('--bgImageAlpha', String(a));
}

function setBgAlpha(v){
  const n = Math.max(10, Math.min(100, Math.round(+v || 100)));
  localStorage.setItem(BGA_KEY, String(n));
  applyBgAlpha(n);
  const el = document.getElementById('setValBga');
  if(el) el.textContent = n + '%';
}

/* 图片先缩放再存 —— 原图直存很容易超 localStorage 配额 */
function shrinkImage(file, cb){
  const fr = new FileReader();
  fr.onload = ()=>{
    const img = new Image();
    img.onload = ()=>{
      const MAX = 1600;
      let w = img.width, h = img.height;
      if(w > MAX || h > MAX){
        const k = MAX / Math.max(w, h);
        w = Math.round(w * k); h = Math.round(h * k);
      }
      try{
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        /*  jpeg 0.82 —— 看得过去，又能把体积压下来 */
        cb(cv.toDataURL('image/jpeg', 0.82));
      }catch(e){
        cb(fr.result);        /* 转不了就用原图 */
      }
    };
    img.onerror = ()=>{ cb(''); };
    img.src = fr.result;
  };
  fr.onerror = ()=>{ cb(''); };
  fr.readAsDataURL(file);
}

/* ── 设置面板里的配色控件 ── */
function renderThemeSwatches(){
  const box = document.getElementById('setTheme');
  if(!box) return;
  const keys = Object.keys(THEMES);
  box.innerHTML = keys.map(k=>{
    const t = THEMES[k];
    return `<div class="th${k === THEME_NAME ? ' on' : ''}" data-th="${k}"
      title="${esc(t.name)}">
      <i style="background:${t.bg}"></i>
      <i style="background:${t.tint.replace(/,/g,' ')};clip-path:
        polygon(0 66%,100% 66%,100% 100%,0 100%)"></i>
    </div>`;
  }).join('');
  if(!box.dataset.bound){
    box.dataset.bound = '1';
    box.addEventListener('click', e=>{
      const el = e.target.closest('.th');
      if(!el) return;
      e.stopPropagation();
      THEME_NAME = el.dataset.th;
      localStorage.setItem(THEME_KEY, THEME_NAME);
      /*  换预设时把「自定义底色」清掉 ——
          不然用户会看到「点了没变化」（自定义色盖住了预设）。 */
      if(CUSTOM_BG){
        CUSTOM_BG = '';
        localStorage.removeItem('mb.bg');
        const tx = document.getElementById('setBgTx');
        if(tx) tx.textContent = '选一个自己喜欢的底色';
      }
      applyTheme(THEME_NAME);
      renderThemeSwatches();
      updateContrastHint();    /* 换预设后报新组合的对比度 */
      const v = document.getElementById('setValTheme');
      if(v) v.textContent = (THEMES[THEME_NAME] || {}).name || '';
    });
  }
  const v = document.getElementById('setValTheme');
  if(v) v.textContent = (THEMES[THEME_NAME] || {}).name || '';
}

/* ── 颜色存档 ──
   挑过的自定义颜色默认留下来（勾选框默认勾选），
   下次想换回去点一下色块就行，不必重新调。
   存 localStorage：mb.bg.arch / mb.fg.arch（JSON 数组，新的在前，最多 12 个）。 */
const ARCH_KEY = { bg:'mb.bg.arch', fg:'mb.fg.arch' };
const ARCH_MAX = 12;

function archLoad(kind){
  try{
    const a = JSON.parse(localStorage.getItem(ARCH_KEY[kind]) || '[]');
    return Array.isArray(a) ? a.filter(x => /^#[\da-f]{6}$/i.test(x)) : [];
  }catch(e){ return []; }
}
function archAdd(kind, hex){
  hex = (hex || '').toUpperCase();
  if(!/^#[\da-f]{6}$/i.test(hex)) return;
  const a = archLoad(kind).filter(x => x !== hex);   /* 去重，新的排最前 */
  a.unshift(hex);
  localStorage.setItem(ARCH_KEY[kind], JSON.stringify(a.slice(0, ARCH_MAX)));
}
function archRemove(kind, hex){
  hex = (hex || '').toUpperCase();
  localStorage.setItem(ARCH_KEY[kind],
    JSON.stringify(archLoad(kind).filter(x => x !== hex)));
}
function renderArch(kind){
  const box = document.getElementById(kind === 'bg' ? 'setBgArch' : 'setFgArch');
  if(!box) return;
  const a = archLoad(kind);
  box.innerHTML = a.map(hex =>
    `<button class="asw" data-arch="${hex}" style="background:${hex}"
       title="${hex} —— 点一下换回来"><span class="asx" data-archx="${hex}"
       title="从存档删除">×</span></button>`).join('');
  if(box.dataset.bound) return;
  box.dataset.bound = '1';
  box.addEventListener('click', e=>{
    e.stopPropagation();
    const x = e.target.closest('[data-archx]');
    if(x){                                   /* × ：从存档删掉 */
      archRemove(kind, x.dataset.archx);
      renderArch(kind);
      toast('已从存档删除', 'ok', '');
      return;
    }
    const sw = e.target.closest('[data-arch]');
    if(!sw) return;                          /* 色块：直接应用 */
    const hex = sw.dataset.arch;
    if(kind === 'bg'){
      applyCustomBg(hex);
      const inp = document.getElementById('setBg');
      const swBox = document.getElementById('setBgSw');
      const tx = document.getElementById('setBgTx');
      if(inp) inp.value = hex;
      if(swBox) swBox.style.background = hex;
      if(tx) tx.innerHTML = `当前 <b>${esc(hex)}</b>`;
    }else{
      applyCustomText(hex);
      const inp = document.getElementById('setFg');
      const swBox = document.getElementById('setFgSw');
      if(inp) inp.value = hex;
      if(swBox) swBox.style.background = hex;
    }
  });
}
/* 取色器「落定」时调用：勾选了存档才存（默认勾着） */
function archMaybeSave(kind, hex){
  const chk = document.getElementById(kind === 'bg' ? 'setBgArchChk' : 'setFgArchChk');
  if(chk && !chk.checked) return;
  archAdd(kind, hex);
  renderArch(kind);
}

function bindThemeControls(){
  if(bindThemeControls._done) return;
  bindThemeControls._done = true;

  /* 自定义底色 */
  const sw = document.getElementById('setBg');
  const swBox = document.getElementById('setBgSw');
  if(sw){
    if(CUSTOM_BG) sw.value = CUSTOM_BG;
    sw.addEventListener('input', ()=>{
      applyCustomBg(sw.value, true);
      if(swBox) swBox.style.background = sw.value;
      const tx = document.getElementById('setBgTx');
      if(tx) tx.innerHTML = `当前 <b>${esc(sw.value.toUpperCase())}</b>`;
    });
    sw.addEventListener('change', ()=>{
      applyCustomBg(sw.value);
      archMaybeSave('bg', sw.value);     /* 存档（勾选框默认勾着） */
      const tx = document.getElementById('setBgTx');
      if(tx) tx.innerHTML = `当前 <b>${esc(sw.value.toUpperCase())}</b>`;
    });
  }
  /* 色块本身也要显示当前色（取色器是透明的） */
  if(swBox) swBox.style.background = CUSTOM_BG || (THEMES[THEME_NAME]||{}).bg || '#f5f1e8';
  const tx = document.getElementById('setBgTx');
  if(tx && CUSTOM_BG) tx.innerHTML = `当前 <b>${esc(CUSTOM_BG.toUpperCase())}</b>`;

  document.getElementById('setBgReset')?.addEventListener('click', e=>{
    e.stopPropagation();
    CUSTOM_BG = '';
    localStorage.removeItem('mb.bg');
    const tx2 = document.getElementById('setBgTx');
    if(tx2) tx2.textContent = '选一个自己喜欢的底色';
    if(swBox) swBox.style.background = (THEMES[THEME_NAME]||{}).bg || '#f5f1e8';
    applyTheme(THEME_NAME);
    updateContrastHint();      /* 跟回预设后，报预设组合的对比度 */
    toast('已跟回预设配色', 'ok', '');
  });

  /* ── 字体颜色 ── */
  const fgSw = document.getElementById('setFg');
  const fgBox = document.getElementById('setFgSw');
  if(fgSw){
    if(CUSTOM_TEXT) fgSw.value = CUSTOM_TEXT;
    fgSw.addEventListener('input', ()=>{
      applyCustomText(fgSw.value, true);
      if(fgBox) fgBox.style.background = fgSw.value;
    });
    fgSw.addEventListener('change', ()=>{
      applyCustomText(fgSw.value);
      archMaybeSave('fg', fgSw.value);   /* 存档（勾选框默认勾着） */
    });
  }
  if(fgBox){
    const cur = getComputedStyle(document.documentElement)
                  .getPropertyValue('--text').trim();
    fgBox.style.background = CUSTOM_TEXT || cur || '#3d3830';
  }
  document.getElementById('setFgAuto')?.addEventListener('click', e=>{
    e.stopPropagation();
    /*  先清掉用户的设定，再让它按当前底色重挑 ——
        这样「自动」永远是「按现在这个底色算」，不是按上次的。 */
    CUSTOM_TEXT = '';
    localStorage.removeItem('mb.fg');
    if(CUSTOM_BG){
      applyCustomBg(CUSTOM_BG, true);
    }else{
      applyTheme(THEME_NAME);
      const s = (THEMES[THEME_NAME]||{}).dark ? DARK_VARS : LIGHT_VARS;
      TEXT_LOCK = { text:s['--text'], muted:s['--muted'], dim:s['--dim'] };
      const tx = document.getElementById('setTextTx');
      if(tx) tx.textContent = '跟着预设走';
    }
    updateContrastHint();      /* 自动挑完了，把新组合的对比度报出来 */
    if(fgSw && TEXT_LOCK) fgSw.value = TEXT_LOCK.text;
    if(fgBox && TEXT_LOCK) fgBox.style.background = TEXT_LOCK.text;
    toast('字体颜色已按底色自动选好', 'ok', '');
  });

  /* 背景图 */
  const file = document.getElementById('setBgiFile');
  document.getElementById('setBgiPick')?.addEventListener('click', e=>{
    e.stopPropagation();
    file?.click();
  });
  file?.addEventListener('change', ()=>{
    const f = file.files && file.files[0];
    if(!f) return;
    shrinkImage(f, url=>{
      if(!url){ toast('这张图读不出来', 'err', ''); return; }
      try{
        localStorage.setItem(BGI_KEY, url);
      }catch(err){
        toast('图片太大，存不下（换张小一点的）', 'err', '');
        return;
      }
      applyBgImage(url);
      document.getElementById('setBgAlphaRow')?.classList.add('show');
      const v = document.getElementById('setValBgi');
      if(v) v.textContent = '已设置';
      toast('背景图片已设置', 'ok', '');
    });
    file.value = '';
  });
  document.getElementById('setBgiClear')?.addEventListener('click', e=>{
    e.stopPropagation();
    localStorage.removeItem(BGI_KEY);
    applyBgImage('');
    document.getElementById('setBgAlphaRow')?.classList.remove('show');
    const v = document.getElementById('setValBgi');
    if(v) v.textContent = '';
    toast('背景图片已移除', 'ok', '');
  });

  /* 图片浓度 */
  const ga = document.getElementById('setBga');
  if(ga){
    const saved = parseInt(localStorage.getItem(BGA_KEY) || '100', 10);
    ga.value = String(Number.isFinite(saved) ? saved : 100);
    setBgAlpha(ga.value);
    ga.addEventListener('input', ()=> setBgAlpha(ga.value));
  }

  /* 颜色存档：把上次存下来的色块渲染出来 */
  renderArch('bg');
  renderArch('fg');
}

/* 开机时把配色装回去 */
function bootTheme(){
  renderThemeSwatches();
  bindThemeControls();
  applyTheme(THEME_NAME);
  /*  背景图读出来要稍等一下（dataURL 可能几百 KB）*/
  setTimeout(()=>{
    try{
      const img = localStorage.getItem(BGI_KEY);
      if(img){
        applyBgImage(img);
        document.getElementById('setBgAlphaRow')?.classList.add('show');
        const v = document.getElementById('setValBgi');
        if(v) v.textContent = '已设置';
      }
    }catch(e){}
  }, 60);
}


/* ══════════ 配色：启动 ══════════
   ★ 时机有讲究 —— app.js 在 index.html 里**位于设置面板之前**，
     直接调 bootTheme() 时 #setTheme 还不存在，色板会渲染成空的。
     （踩过：页面上色板一个都没有，但手动调一次函数就出来了。）

     分两半：
       · 变量要立刻写 —— 晚了会看到一瞬间的默认配色（闪一下）
       · 控件要等 DOM —— 面板还没解析出来，找不到元素 */
(function bootThemeNow(){
  /*  ① 变量立刻生效（此时 :root 一定在） */
  try{
    applyTheme(THEME_NAME);
    renderThemeSwatches();      /* 面板不在就什么也不做 */
  }catch(e){}
})();

function bootThemeLater(){
  if(bootThemeLater._done) return;
  const host = document.getElementById('setTheme');
  if(!host) return;
  bootThemeLater._done = true;
  /*  首屏配色已经写过了，这里只补控件 + 背景图 */
  renderThemeSwatches();
  bindThemeControls();
  /*  字色控件同步一下当前值（可能是上次存的） */
  const fgSw = document.getElementById('setFg');
  const fgBox = document.getElementById('setFgSw');
  const cur = TEXT_LOCK ? TEXT_LOCK.text
            : getComputedStyle(document.documentElement)
                .getPropertyValue('--text').trim();
  if(fgSw && cur) fgSw.value = cur;
  if(fgBox && cur) fgBox.style.background = cur;
  const tx = document.getElementById('setTextTx');
  if(tx){
    tx.textContent = CUSTOM_TEXT ? ('当前 ' + CUSTOM_TEXT.toUpperCase())
                   : (CUSTOM_BG ? '已按底色自动选' : '跟着预设走');
  }
  try{
    const img = localStorage.getItem(BGI_KEY);
    if(img){
      applyBgImage(img);
      document.getElementById('setBgAlphaRow')?.classList.add('show');
      const v = document.getElementById('setValBgi');
      if(v) v.textContent = '已设置';
    }
  }catch(e){}
}
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', bootThemeLater);
}else{
  bootThemeLater();
}
/*  双保险：DOMContentLoaded 早就过了也不会漏（同 bindSettings 的做法） */
setTimeout(bootThemeLater, 0);
setTimeout(bootThemeLater, 800);


/* ══════════════ 深色主题标记 ══════════════
   原本这里还有一整套「鼠标高光随动」——
   鼠标在卡片上滑动时，一层柔光跟着走，写 --mx / --my 两个变量。

   ★ 已按要求**删除**（用户：「删除鼠标高光随动」）。
     连监听器一起摘掉：这类「每次 pointermove 都要读一次
     getBoundingClientRect」的写法，在窄窗口里
     是实打实的每帧强制重排来源，去掉之后更稳。

   ★ 只保留下面这个「打深色标记」的小函数 ——
     它没有性能开销，而且深色主题下别的地方（面板、滚动条）
     也可能想用这个标记。 */

/*  ── 深色主题下高光要淡一点 ──
      白高光铺在黑底上很扎眼（像一块污渍），
      降到一半才像「反光」。
      CSS 里也有 @media 那种写法，但主题是 JS 切的，
      所以在这里打个标记，让 CSS 用属性选择器接上。 */
function lqSyncDark(){
  const isDark = !!(THEMES[THEME_NAME] || {}).dark;
  document.documentElement.setAttribute('data-dark', isDark ? '1' : '0');
}


/* ══════════ 深色标记：启动 ══════════ */
lqSyncDark();


/* ══════════════ 最大化流畅性 ══════════════
   窗口尺寸变化的那一小段时间，把最贵的视觉效果临时关掉
   （规则在 app.css 的 `html.resizing` 那一段）。

   为什么需要：
      页面上有 46 处 backdrop-filter。单看每一处都不贵，
      但最大化那一刻「尺寸变 9.5 倍 × 46 处重新采样」，
      加起来就是肉眼可见的顿。

   怎么判断「变得差不多了」：
      用 debounce —— resize 停稳 220ms 之后才摘掉 .resizing。
      拖拽过程中会一连串触发 resize，每次都会把定时器推后，
      所以拖的过程中一直保持「轻量模式」，松手才恢复。

   ★ 为什么不用 transitionend / animationend：
     窗口 resize 不触发这些事件，只能靠 debounce。 */
let _rszLightT = null;
let _rszLightOn = false;

function lightenDuringResize(){
  if(!_rszLightOn){
    _rszLightOn = true;
    document.documentElement.classList.add('resizing');
  }
  clearTimeout(_rszLightT);
  _rszLightT = setTimeout(()=>{
    _rszLightOn = false;
    document.documentElement.classList.remove('resizing');
  }, 220);
}

/*  捕获阶段监听 —— resize 不冒泡，普通监听挂在 window 上就行；
    这里用 passive 是因为我们只读不写事件本身。 */
window.addEventListener('resize', lightenDuringResize, {passive:true});

/*  最大化/还原的瞬间立刻进入轻量模式 ——
    不等 resize 事件（那要等窗口开始变才来），
    先把模糊关掉，动完再开，观感更顺。 */
function markMaximizing(){
  if(!_rszLightOn){
    _rszLightOn = true;
    document.documentElement.classList.add('resizing');
  }
  clearTimeout(_rszLightT);
  _rszLightT = setTimeout(()=>{
    _rszLightOn = false;
    document.documentElement.classList.remove('resizing');
  }, 420);       /* 最大化比一般 resize 慢一点，时间给长些 */
}


/* 最大化时先关模糊再动（见 markMaximizing 的说明） */
(function patchMaximize(){
  const b = document.getElementById('btnWinMax');
  if(!b || b.dataset.lqPatched) return;
  b.dataset.lqPatched = '1';
  /*  捕获阶段插一个「提前」的动作 ——
      原来的 click 处理是 async（要等后端返回），
      所以在它之前先把轻量模式打开。 */
  b.addEventListener('click', ()=> markMaximizing(), true);
})();


/* ══════════════ 使用教程 ══════════════
   逐条介绍每个可交互点：把元素圈出来 + 旁边浮一张说明卡。

   ★ 用户的原话：
     「esc 之类太繁琐了，安装包里额外加初次使用页面教程，
      指明几乎每一个可交互点的作用。可跳过，可在设置里面再次唤起。」

   ★ 为什么拆成「步骤数组」而不写死一段 HTML：
     增删步骤只要动数组；排版、定位、进度、按钮全是共用的。
     而且以后加了新按钮，往数组里补一条就行。
   ══════════════════════════════════════════════════════════════════ */

const TOUR_KEY = 'mb.tour.done';

/*  ── 教程步骤 ──
    sel 为空 = 不圈元素，卡片居中（讲的是「没有具体按钮」的事，
    比如快捷键）。 */
const TOUR_STEPS = [
  {
    sel: '',
    t: '先看一圈',
    b: `<p>这里用一分钟把每个能点的地方过一遍。</p>
        <p class="dim">随时可以点「跳过」—— 以后在
        <b>设置</b>里还能再叫出来。</p>`,
  },
  {
    sel: '#bar .brand',
    t: '顶栏：这是你的数据',
    b: `<p>最左边显示 <b>上次更新时间</b>。</p>
        <p>旁边那排圆按钮从左往右是：</p>
        <ul>
          <li><b>折叠</b> —— 把所有区块一起收起 / 展开</li>
          <li><b>倒计时</b> —— 点一下立刻刷新</li>
          <li><b>暂停键</b> —— 开关自动刷新</li>
          <li><b>信封</b> —— 晚报（工作日晚 9 点后生成）</li>
          <li><b>钥匙</b> —— 重新登录</li>
          <li><b>Teams</b> —— 打开 Teams / EC 面板</li>
          <li><b>齿轮</b> —— 设置（外观 / 操作）</li>
          <li><b>收起</b> —— 缩成小浮窗</li>
        </ul>`,
  },
  {
    sel: '.winbtns',
    t: '右上角：窗口按钮',
    b: `<p>最小化 / 最大化 / 关闭。</p>
        <p class="dim">窗口没有系统标题栏，所以这三个是唯一的
        关窗途径。整条顶栏都能拖动窗口。</p>
        <div class="tr-tip">${icon('i-key','sm')}
          <span>按 <kbd>Esc</kbd> 也能唤出这几个操作（含「退出程序」）。
          万一界面卡住点不到按钮，它是第二道保险。</span></div>`,
  },
  {
    sel: '#tabs',
    t: '五个页签',
    b: `<p>课程成绩 / 今日课表 / GPA / 资料 / Teams。</p>
        <p class="dim">每个页签右边的小数字是「这个页有几件事」。<br>
        左右滑动、横向滚轮、方向键也能切页。</p>`,
  },
  {
    sel: '#stats',
    t: '六个关键数字',
    b: `<p>待完成 / 已过期 / 7 天内 / 已完成 / 平均分 / GPA。</p>
        <p class="dim">GPA 优先用学校公布的；没有就按你的分数
        本机估算，并标明「估算」。</p>`,
  },
  {
    sel: '#page-grade .card, #page-grade .grid',
    t: '任务卡片',
    b: `<p>每张卡是一个要交的作业。左侧的色条按紧急程度变色：
        过期红、3 天内橙、7 天内黄、更远绿。</p>
        <p><b>鼠标移上去</b>右上角会冒出按钮：</p>
        <ul>
          <li><b>我已完成</b> —— 线下交了 / 老师没给分，
              标记后不再提示（只在本机生效）</li>
          <li><b>下载</b> —— 有附件的任务才有这个键</li>
        </ul>
        <p class="dim">点卡片本身会展开详情面板。完成的任务，
        状态会显示在<b>右上角</b>（不再是挤在底部标签里）。</p>`,
  },
  {
    sel: '#live',
    t: '自动刷新',
    b: `<p>默认每 <b>5 分钟</b>自动抓一次。</p>
        <p class="dim">那个圆按钮上的数字是倒计时，点一下立刻刷新。</p>`,
  },
  {
    sel: '#btnDigest',
    t: '晚报',
    b: `<p>每个工作日晚上 9 点后自动生成一份当日小结：
        今天新出了什么、明天有什么要交。</p>
        <p class="dim">有新的会挂红点。</p>`,
  },
  {
    sel: '#btnTeams',
    t: 'Teams / EC',
    b: `<p>点开是 Teams 面板 —— 把各个团队频道里的消息汇总到一处，
        可以搜索。</p>
        <div class="tr-tip">${icon('i-check','sm')}
          <span>如果你在设置里填过<b>「EC 名单里的名字」</b>，
          每天出的 EC 名单会自动扫一遍，上面有你就会提醒。</span></div>`,
  },
  {
    sel: '#btnSettings',
    t: '设置',
    b: `<p>外观（圆角 / 透明度 / 毛玻璃 / <b>六套配色</b> /
        自定义底色和背景图 / 字体颜色）都在这里。</p>
        <p><b>操作</b>区在最底下：立即刷新、数据与备份。</p>
        <p class="dim">「窗口形态」也在设置里 —— 可以切小浮窗。</p>`,
  },
  {
    sel: '#btnWinMin',
    t: '收成小浮窗',
    b: `<p>点它就缩成一个小方块，浮在桌面上。</p>
        <p>小浮窗里只放最要紧的三样：<b>下一节课</b>、
        几个关键数字、三个按钮（刷新 / 置顶 / 展开）。</p>
        <p class="dim">还能拖边缘调大小；设置里可以开「固定在其他窗口上层」。</p>`,
  },
  {
    sel: '',
    t: '两个快捷键',
    b: `<p><kbd>Esc</kbd> —— 关掉最上层的东西（面板 / 教程）；
        都没开的时候，唤出<b>窗口菜单</b>（最小化 / 关闭 / 退出）。</p>
        <p><kbd>←</kbd> <kbd>→</kbd> —— 切换五个页签。</p>
        <p class="dim">双击顶栏空白处 = 最大化 / 还原。</p>`,
  },
  {
    sel: '',
    t: '好了',
    b: `<p>就这些。</p>
        <p class="dim">以后想再看一遍：设置面板最底下有个
        <b>「看一遍使用教程」</b>。</p>`,
  },
];

let trIdx = 0;

function trEl(id){ return document.getElementById(id); }

/*  把某一页摆出来：圈目标 + 放卡片 + 填文字 */
function trShow(i){
  const host = trEl('tour');
  if(!host) return;
  const steps = TOUR_STEPS;
  trIdx = Math.max(0, Math.min(steps.length - 1, i));
  const st = steps[trIdx];

  trEl('trStep').textContent = (trIdx + 1) + ' / ' + steps.length;
  trEl('trTitle').textContent = st.t;
  /*  正文里嵌了 icon() 生成的 SVG ——
      它是我们自己的模板，不是用户输入，直接 innerHTML 是安全的。 */
  trEl('trBody').innerHTML = st.b;

  trEl('trPrev').disabled = (trIdx === 0);
  trEl('trNext').textContent =
    (trIdx === steps.length - 1) ? '开始使用' : '下一步';

  /*  ── 定位 ── */
  const spot = trEl('trSpot');
  const card = trEl('trCard');
  let box = null;

  if(st.sel){
    const t = document.querySelector(st.sel);
    if(t){
      /*  ★ getBoundingClientRect 给的是**视口坐标**，
          而 #tour 是 fixed 定位 —— 两者的参照系一致，
          所以直接用，不用加 scrollTop。 */
      const r = t.getBoundingClientRect();
      if(r.width > 1 && r.height > 1){
        box = {x: r.left, y: r.top, w: r.width, h: r.height};
      }
    }
  }

  if(box){
    const PAD = 6;                       // 圈稍微比元素大一点，好看
    host.classList.add('has-spot');
    spot.style.display = 'block';
    spot.style.left = (box.x - PAD) + 'px';
    spot.style.top = (box.y - PAD) + 'px';
    spot.style.width = (box.w + PAD * 2) + 'px';
    spot.style.height = (box.h + PAD * 2) + 'px';
  }else{
    /*  ★ 没圈元素时不能靠 .tr-spot 的阴影来压暗屏幕：
          它的阴影是从元素矩形往外扩 9999px，
          元素被塞到角落的话只盖住一块，屏幕还是亮的。
          所以改用 .tr-mask（满屏半透明），.tr-spot 收掉。 */
    host.classList.remove('has-spot');
    spot.style.display = 'none';
  }

  /*  ── 卡片位置 ──
      规则：目标在上半屏 → 卡放下面；反之放上面。
      没目标 → 居中。
      左右尽量贴着目标那一侧，但保证不出界。 */
  const CW = Math.min(330, window.innerWidth - 24);
  const CH = card.offsetHeight || 220;
  const M = 12;
  let cx, cy;

  if(box){
    const midY = box.y + box.h / 2;
    if(midY < window.innerHeight / 2){
      cy = box.y + box.h + M;                      // 卡放下面
    }else{
      cy = box.y - CH - M;                         // 卡放上面
    }
    cx = Math.min(Math.max(M, box.x), window.innerWidth - CW - M);
    /*  上下都放不下 → 放右侧那半边 */
    if(cy < M || cy + CH > window.innerHeight - M){
      cy = Math.max(M, Math.min(window.innerHeight - CH - M,
                                window.innerHeight / 2 - CH / 2));
      cx = (box.x + box.w / 2 < window.innerWidth / 2)
        ? Math.min(window.innerWidth - CW - M, box.x + box.w + M)
        : Math.max(M, box.x - CW - M);
    }
  }else{
    cy = window.innerHeight / 2 - CH / 2;
    cx = window.innerWidth / 2 - CW / 2;
  }
  card.style.left = Math.round(cx) + 'px';
  card.style.top = Math.round(Math.max(M, cy)) + 'px';
}

function openTour(fromStart){
  const host = trEl('tour');
  if(!host) return;
  /*  ★ 教程开着的时候先把其它浮层关掉 ——
      叠在一起会互相盖住，用户不知道在看哪个。 */
  try{
    closeWinMenu();
    closeSettings();
    closeDigest();
    closeData();
    closeTeams();
    closeCustomPanel();
  }catch(e){}
  host.classList.add('open');
  document.body.classList.add('touring');
  trShow(fromStart ? 0 : trIdx);
}

function closeTour(markDone){
  const host = trEl('tour');
  if(host) host.classList.remove('open');
  document.body.classList.remove('touring');
  if(markDone) tourMarkDone();
}

/*  ── 记下「看过了」──
    ★ 两边都记：
        · localStorage —— 快，下次启动立刻能读到
        · 后端 window.json —— 稳，用户清了 WebView2 缓存也还在
      弹之前两边都问一遍，只要有一边记得就不再弹。 */
function tourMarkDone(){
  try{ localStorage.setItem(TOUR_KEY, '1'); }catch(e){}
  const a = window.pywebview && window.pywebview.api;
  if(a && typeof a.tour_done === 'function'){
    try{ a.tour_done(); }catch(e){}
  }
}

/*  当前是不是「已经看过了」。先同步给一个初值（localStorage），
    再由 maybeAutoTour() 异步向后端确认一次。

    ★ 为什么不能只靠 localStorage：
      之前 pywebview 是隐私模式，localStorage **每次启动都是空的**，
      于是「看过了」根本记不住、教程每次启动都弹。
      现在虽然开了持久化（见 widget.py 的 webview.start），
      但多一个服务端副本更保险。 */
function tourSeen(){
  try{ return !!localStorage.getItem(TOUR_KEY); }catch(e){ return false; }
}

function bindTour(){
  if(bindTour._done) return;
  bindTour._done = true;

  trEl('trNext')?.addEventListener('click', ()=>{
    if(trIdx >= TOUR_STEPS.length - 1){ closeTour(true); return; }
    trShow(trIdx + 1);
  });
  trEl('trPrev')?.addEventListener('click', ()=> trShow(trIdx - 1));
  trEl('trSkip')?.addEventListener('click', ()=> closeTour(true));
  trEl('trClose')?.addEventListener('click', ()=> closeTour(true));

  /*  点遮罩空白处也能退（跟其它浮层的行为一致） */
  trEl('tour')?.addEventListener('click', e=>{
    if(e.target === trEl('tour') || e.target === trEl('trMask')){
      closeTour(true);
    }
  });

  /*  窗口尺寸变了要重新摆（不然圈会错位） */
  window.addEventListener('resize', ()=>{
    if(trEl('tour')?.classList.contains('open')) trShow(trIdx);
  }, {passive:true});
}

/*  ── 首次自动弹 ──
    ★ 时机：**引导页关了之后**才弹。
      两个浮层叠一起会互相盖住，用户也不知道先看哪个。
      所以这里轮询等「引导页不在 / 已经填过账号」。

    ★ 判「要不要弹」要问两边（localStorage + 后端），
      只要有一边记得看过，就安静地不弹。 */
function maybeAutoTour(){
  if(tourSeen()) return;

  let tries = 0;
  const tick = ()=>{
    tries++;
    const su = document.getElementById('setup');
    const suOpen = !!(su && su.classList.contains('open'));
    /*  引导页还开着 → 等；等超过 60 秒也放弃（用户可能在慢慢填）*/
    if(suOpen && tries < 120){ setTimeout(tick, 500); return; }
    if(suOpen) return;

    /*  向后端确认一次「到底看过没有」。
        ★ 这里是异步的，所以「等」不能写在外层 ——
          不然用户没配 pywebview 时永远等不到。 */
    const a = window.pywebview && window.pywebview.api;
    if(a && typeof a.tour_status === 'function'){
      try{
        a.tour_status().then(r=>{
          if(r && r.done){
            /*  后端说看过 → 同步回 localStorage（省得下次再问），
                但不弹。 */
            try{ localStorage.setItem(TOUR_KEY, '1'); }catch(e){}
            return;
          }
          openTour(true);
        }).catch(()=> openTour(true));
        return;
      }catch(e){}
    }
    openTour(true);
  };
  setTimeout(tick, 1200);
}

/*  ── 设置里的「看一遍教程」按钮 ── */
function bindTourEntry(){
  const b = document.getElementById('opTour');
  if(b && !b.dataset.bound){
    b.dataset.bound = '1';
    b.addEventListener('click', ()=> openTour(true));
  }
}


/* ══════════ 使用教程：启动 ══════════ */
bindTour();
(function tourBoot(){
  /*  和别的设置面板入口一样：等 DOM 解析完再接按钮
      （app.js 在 index.html 里位于面板之前）。 */
  const go = ()=>{
    bindTourEntry();
  };
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', go);
  }else{
    setTimeout(go, 0);
  }
  setTimeout(go, 800);
})();
maybeAutoTour();


/* ══════════════ EC 名单关注 ══════════════
   用户原话：
     「可否直接在输入帐密的注册环节索要可能出现在 ec 里面的名字，
      每回出名单自动扫描，有的话发通知」

   ★ 为什么在注册时问：
     EC 名单是每天出的 PDF，上面写的是**学校系统里的名字**。
     用户自己知道那个名字长什么样（可能是拼音、可能带中间名），
     但程序猜不到 —— 与其事后让他去设置里配，不如注册时顺手问一句。

   ★ 名字可以填好几个：
     中文名、拼音全拼、姓在前 / 名在前的写法……
     任何一个命中就算命中。分隔符随便用（逗号、空格、换行、斜杠）。
   ══════════════════════════════════════════════════════════════════ */

function apiEcStatus(){
  if(window.pywebview?.api?.ec_watch_status)
    return window.pywebview.api.ec_watch_status();
  return Promise.resolve({ok:false});
}
function apiEcSave(names, notify){
  if(window.pywebview?.api?.ec_watch_save)
    return window.pywebview.api.ec_watch_save(names, !!notify);
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}
function apiEcScan(){
  if(window.pywebview?.api?.ec_watch_scan)
    return window.pywebview.api.ec_watch_scan();
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

let EC_ON = false;          // 有没有配过名字
let EC_ALERT = false;       // 最近一次扫描命中了吗

/*  ── 顶栏那个红点 ──
      挂在 Teams 键上。为什么是它：EC 名单是从 Teams 里下载的 PDF，
      和那个面板是同一件事，用户看到红点第一反应就是点进去。
      写法沿用晚报那个 .digdot：静态节点 + 切 .on 类，
      不去增删 DOM（增删会让每次轮询都触发一次重排）。 */
function ecDrawDot(){
  const d = document.getElementById('ecdot');
  const b = document.getElementById('btnTeams');
  if(!d || !b) return;
  d.classList.toggle('on', EC_ALERT);
  if(EC_ALERT){
    b.title = 'EC 名单上有你';
    b.classList.add('dig');
  }else{
    b.title = 'Teams / EC';
    b.classList.remove('dig');
  }
}

/*  ── 设置面板里把当前状态显示出来 ── */
function ecRenderSet(st){
  const note = document.getElementById('setEcNote');
  const val = document.getElementById('setValEc');
  if(!note) return;
  if(!st || !st.ok){
    note.textContent = '';
    if(val) val.textContent = '';
    return;
  }
  if(val){
    val.textContent = st.n_names
      ? (st.n_names + ' 个名字')
      : '还没填';
  }
  /*  最近一次扫描的结果 —— 说清「扫了几份」「什么时候扫的」，
      免得用户以为是没生效。
      ★ st.last 是**一个对象**（{ok,names,scanned,hit,found,at}），
        不是字符串 —— 别拿它去 String()，那会显示成 [object Object]。
        真正要显示的是 st.at 和 st.scanned。 */
  const when = st.at ? String(st.at).replace('T', ' ').slice(0, 16) : '';
  const n = st.scanned || 0;
  if(st.found && st.found.length){
    note.innerHTML = icon('i-check','sm inl') + ' 在名单上找到：<b>'
      + st.found.map(esc).join('、') + '</b>'
      + (when ? '<span class="dim">（' + esc(when) + '）</span>' : '');
    note.className = 'ec-note hit';
  }else if(n){
    note.innerHTML = '最近检查了 ' + n + ' 份名单 PDF，没有你'
      + (when ? '<span class="dim">（' + esc(when) + '）</span>' : '');
    note.className = 'ec-note';
  }else{
    note.innerHTML = st.n_names
      ? '还没扫到名单 PDF。出名单后会自动检查。'
      : '填上名字后，每天出的名单 PDF 会自动扫一遍。';
    note.className = 'ec-note';
  }
}

/*  ══════════ EC 命中片段（0.5.2）══════════

    以前命中只给「有你 / 没你」，用户还得自己打开 PDF、Ctrl+F 找。
    这里把命中处附近的原文摆出来，顺便把里面认得出的
    时间 / 地点 / 组别标成小标签。

    ★ 三条原则，写在代码里免得以后被“优化”掉：
      ① 只**照抄原文**，不补全、不推测。名单没写地点就没地点栏。
      ② 片段可能截在句子中间，所以标明「…」并且不修饰。
      ③ 下面那句提醒（未匹配不代表不在名单中）**必须留着** ——
        文字匹配不是名单核验，用户要自己核对原名单。
 */
function sigChips(sig){
  if(!sig) return '';
  const out = [];
  (sig.times || []).forEach(t=> out.push(`<span class="ecsig t">${esc(t)}</span>`));
  (sig.places || []).forEach(t=> out.push(`<span class="ecsig p">${esc(t)}</span>`));
  (sig.groups || []).forEach(t=> out.push(`<span class="ecsig g">${esc(t)}</span>`));
  return out.join('');
}

function ecRenderDetails(st){
  const box = document.getElementById('ecDetails');
  if(!box) return;
  const ds = (st && st.details) || [];
  if(!ds.length){
    if(box.innerHTML) box.innerHTML = '';
    box.hidden = true;
    return;
  }
  const h = ds.map(d=>{
    const chips = sigChips(d.signals);
    /*  来源写清楚是「哪份 PDF」—— 名单在附件里，不在消息正文里，
        所以显示文件名比显示消息标题有用得多。 */
    const src = [d.file, d.where, d.at].filter(Boolean).join(' · ');
    return `<div class="ecdet">
      <div class="ecdet-h">
        <b>${esc(d.name || '')}</b>
        ${src?`<span class="ecdet-s" title="取自这条路名单 PDF 的文字">${esc(src)}</span>`:''}
      </div>
      ${chips?`<div class="ecdet-c">${chips}</div>`:''}
      <div class="ecdet-t">…${esc(d.text || '')}…</div>
    </div>`;
  }).join('');
  const note = esc((st && st.note) ||
    '按文字匹配。未匹配不代表不在名单中。以学校原名单为准。');
  const src2 = `<div class="ecdet-n">${icon('i-file','sm inl')}`
    + `名单来自附件里的 PDF，不是消息正文。下面是 PDF 里命中处的原文。`
    + `</div>`;
  const nh = `${h}<div class="ecdet-n">${icon('i-alert','sm inl')}${note}</div>` + src2;
  /*  每秒轮询会走到这 —— 先比对再写，别白重排一遍 */
  if(box.innerHTML !== nh) box.innerHTML = nh;
  box.hidden = false;
}

async function ecRefresh(){
  try{
    const st = await apiEcStatus();
    EC_ON = !!(st && st.n_names);
    EC_ALERT = !!(st && st.alert);
    ecDrawDot();
    ecRenderSet(st);
    ecRenderDetails(st);
    /*  Teams 面板里的 EC 状态行（0.5.1：EC 并入 Teams 板块）。
        一秒一轮的轮询也会走到这 —— 先比对再写，别触发重排。 */
    const tm = document.getElementById('tmEc');
    if(tm){
      let h, cls;
      if(st && st.ok && st.found && st.found.length){
        h = icon('i-check','sm inl') + ' EC 名单上有你：<b>'
          + st.found.map(esc).join('、') + '</b>'
          + '<span class="dim"> — 点看 PDF 原文片段</span>';
        cls = 'tm-ec hit';
      }else if(st && st.ok && st.n_names){
        h = 'EC 关注 ' + st.n_names + ' 个名字'
          + (st.scanned ? '，最近扫了 ' + st.scanned + ' 份名单 PDF，没你'
                        : '，还没扫到名单 PDF');
        cls = 'tm-ec';
      }else{
        h = '还没设置 EC 关注名字 —— 点这行去设置';
        cls = 'tm-ec';
      }
      if(tm.innerHTML !== h) tm.innerHTML = h;
      if(tm.className !== cls) tm.className = cls;
    }
    /*  设置里那个输入框：只在用户还没动过的时候回填，
        免得把正在编辑的内容冲掉。 */
    const inp = document.getElementById('setEcNames');
    if(inp && !inp.dataset.dirty && st && st.names){
      inp.value = (st.names || []).join('、');
    }
  }catch(e){}
}

function bindEcEntry(){
  /*  Teams 面板里那行 EC 状态：整行可点 → 打开设置面板改名字 */
  const tm = document.getElementById('tmEc');
  if(tm && !tm.dataset.bound){
    tm.dataset.bound = '1';
    tm.addEventListener('click', ()=> openSettings());
  }
  const inp = document.getElementById('setEcNames');
  if(inp && !inp.dataset.bound){
    inp.dataset.bound = '1';
    inp.addEventListener('input', ()=>{ inp.dataset.dirty = '1'; });
  }
  const sv = document.getElementById('setEcSave');
  if(sv && !sv.dataset.bound){
    sv.dataset.bound = '1';
    sv.addEventListener('click', async ()=>{
      const raw = (inp && inp.value) || '';
      const old = sv.textContent;
      sv.disabled = true;
      sv.textContent = '保存中…';
      try{
        const r = await apiEcSave(raw, true);
        if(r && r.ok){
          if(inp) delete inp.dataset.dirty;
          sv.textContent = '已保存';
          setTimeout(()=>{ sv.textContent = old; sv.disabled = false; }, 1200);
          ecRefresh();
          return;
        }
        /*  出错也要把原因说出来 —— 光变回原样用户会以为没点上 */
        const n = document.getElementById('setEcNote');
        if(n){
          n.textContent = (r && r.error) || '保存失败';
          n.className = 'ec-note bad';
        }
      }catch(e){}
      sv.textContent = old;
      sv.disabled = false;
    });
  }
  const sc = document.getElementById('setEcScan');
  if(sc && !sc.dataset.bound){
    sc.dataset.bound = '1';
    sc.addEventListener('click', async ()=>{
      const old = sc.textContent;
      sc.disabled = true;
      sc.textContent = '检查中…';
      const n = document.getElementById('setEcNote');
      try{
        const r = await apiEcScan();
        if(r && r.ok){
          if(r.found && r.found.length){
            if(n){
              n.innerHTML = icon('i-check','sm inl') + ' 找到：<b>'
                + r.found.map(esc).join('、') + '</b>';
              n.className = 'ec-note hit';
            }
          }else if(n){
            n.textContent = r.scanned
              ? ('扫了 ' + r.scanned + ' 份名单，没有你')
              : '本机还没有 EC 名单。先在 Teams 面板里抓一次。';
            n.className = 'ec-note';
          }
        }else if(n){
          n.textContent = (r && r.error) || '检查失败';
          n.className = 'ec-note bad';
        }
      }catch(e){}
      sc.textContent = old;
      sc.disabled = false;
      ecRefresh();
    });
  }
}

/*  ── 低频轮询 ──
      为什么低频：名单一天出一份，没必要勤查。
      而且这个只读本地文件，几乎不耗资源。 */
let ecTimer = null;
function startEcTick(){
  ecRefresh();
  if(ecTimer) return;
  ecTimer = setInterval(ecRefresh, 5 * 60 * 1000);
}


/* ══════════ EC：启动 ══════════ */
(function ecBoot(){
  const go = ()=>{
    bindEcEntry();
    startEcTick();
  };
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', go);
  }else{
    setTimeout(go, 0);
  }
  setTimeout(go, 900);
})();

/* ══════════════ 液态玻璃 v2（静态玻璃 + 缓慢流光）══════════════
   v0.5.0 改版，两个原因：

     · 用户要求「删除鼠标高光随动」—— 跟鼠标的那套已经拆掉
     · 用户要求「试着重置液态玻璃……粒子、折射、流动、高光，
       但不要太夸张」

   现在的玻璃质感是**四层静态叠加**（CSS 里写的）：
       ① 斜向偏色   ← 折射感
       ② 顶部棱边反光 ← 高光
       ③ 磨砂颗粒   ← 粒子
       ④ 缓慢流光   ← 流动（就是这里插的这个元素）

   ★ 为什么流光单独用一个子元素，而不是第三个伪元素
     伪元素只有两个（::before / ::after），已经用完了：
       ::before 画「偏色 + 颗粒」，::after 画「棱边反光」。
     而且流光要做 `transform` 位移动画，和上面那些
     `background` 抢同一层会打架。

   ★ 为什么只给卡片不给按钮
     按钮最小才 28×26。一条 102 度的斜光带在这么小的框里
     会被裁成一条生硬的斜线，看起来像划痕，不像光。
     所以按钮只保留偏色 + 棱边反光。

   ★ 为什么不做真折射
     真折射要用 SVG displacement map（WebView2 支持不稳）
     或者 WebGL（包体、耗电都不划算）。
     用户也说了「不要太夸张」，所以走视觉等效。
    ────────────────────────────────────────────────────────── */

/*  ── 贴 `.lq` 类 ──
   ★★ 这一步不能漏（真踩过）：
      CSS 里所有玻璃效果都挂在 `.lq` 这个类上。
      上一版是 `lqDecorate()` 负责贴类，删「鼠标随动」时
      把它一起删了 → `.lq` 永远不出现 →
      `--lqA` 是空的、`::before` 的 background 是 none，
      看起来「玻璃整个失效了」。
      → 贴类和「跟鼠标」是两件事，前者必须留下。

   ★★ 为什么**不能**给浮层面板贴这个类（这里真出过事故）
      `.lq` 规则里有 `position:relative`（玻璃层要相对它定位）。
      而 `.digwrap` / `.clwrap` 这些浮层面板自己写的是
      `position:absolute` + `top/bottom` 撑满屏幕。

      两条规则**特异度相同**（都是一个类），
      而 `.lq` 在文件里排在后面 → **它赢** →
      面板变成 `position:relative`。

      症状（实测）：`top/bottom` 从「撑满」变成「相对偏移」，
      高度于是塌成内容高度 —— 晚报面板在小浮窗里量出
      `502x1711`，而视口只有 `518x276`。
      用户看到的就是「晚报显示不完整」。

      → 所以**浮层面板一律不贴**。它们本来就有自己的玻璃底
        （`--glass-3` + `backdrop-filter`），不需要这一套。 */
const LQ_SEL = [
  '.card', '.grow', '.tm-item', '.stat',
].join(',');

/*  流动层也给同一批元素（面板不在内，理由同上） */
const LQ_FLOW_SEL = LQ_SEL;

function lqMark(root){
  const scope = root || document.body;
  if(!scope || !scope.querySelectorAll) return;
  let list;
  try{
    list = scope.querySelectorAll(LQ_SEL);
  }catch(e){ return; }
  list.forEach(el=>{
    if(!el.classList.contains('lq')) el.classList.add('lq');
  });
}

/*  ── 给指定范围里的卡片补上流动层 ──
    ★ 先查 `:not(:has(.lq-eff))` ——
      没有新元素时 querySelectorAll 直接返回空，很便宜。
      （`:has()` 在 WebView2/Chromium 105+ 都有，
        这个项目用的 WebView2 是 153，没问题。） */
function lqFlow(root){
  if(!document.body) return;
  const scope = root || document.body;

  /*  ① 先保证类贴上了（s 写在前面，别调换顺序） */
  lqMark(scope);

  /*  ② 再补流动层 */
  let list;
  try{
    list = scope.querySelectorAll(LQ_FLOW_SEL);
  }catch(e){ return; }

  list.forEach(el=>{
    /*  已经有过就跳过 —— 幂等，重复调用不会插两个 */
    if(el.querySelector(':scope > .lq-eff')) return;
    const eff = document.createElement('i');
    eff.className = 'lq-eff';
    eff.setAttribute('aria-hidden', 'true');
    el.appendChild(eff);
  });

  /*  ③ 挂随动监听（幂等） */
  lqBindMove(scope);
}

/*  ── 鼠标随动（按窗口大小调强度）──
   ★★ 读数 / 写值分离：
      pointermove 一秒能上百次。如果每次都「读 getBoundingClientRect
      → 写 style」，浏览器每帧都得先算完样式才能给出准确尺寸 ——
      每帧一次强制同步重排。
      所以移动时只**记坐标**，真正的写放到 rAF 里，一帧一次。

   ★ 只给面积大的元素挂（卡片 / 统计块）。
     按钮不挂：28x26 的框里那点光斑几乎看不见，
     白付一份监听开销。

   ★ 反光**强度**不在这里算 ——
     它随窗口宽度变化，用 CSS 的 @media + calc() 写在 app.css 里，
     比 JS 监听 resize 更准也更省（浏览器自己重算，不用我们介入）。 */
const LQ_MOVE_SEL = ['.card', '.grow', '.tm-item', '.stat'].join(',');

let _lqEl = null, _lqX = 0, _lqY = 0, _lqQueued = false;

/*  移动：只记坐标，写值交给 rAF */
function lqMove(e){
  const el = e.currentTarget;
  _lqEl = el;
  _lqX = e.clientX;
  _lqY = e.clientY;
  if(_lqQueued) return;
  _lqQueued = true;
  requestAnimationFrame(lqFlush);
}

function lqFlush(){
  _lqQueued = false;
  const el = _lqEl;
  if(!el) return;
  try{
    const r = el.getBoundingClientRect();
    if(!r.width || !r.height) return;
    /*  写成百分比 —— 窗口大小变了也不用重算 */
    const x = ((_lqX - r.left) / r.width * 100).toFixed(1) + '%';
    const y = ((_lqY - r.top) / r.height * 100).toFixed(1) + '%';
    el.style.setProperty('--mx', x);
    el.style.setProperty('--my', y);
  }catch(err){}
}

/*  离开：把内联变量**删掉** ——
    删掉后 CSS 里 `.lq` 上的默认值（26% / -8%）接管。
    ★ 因为 --mx 注册过 @property，这个「回到默认位置」
      也走 transition，是**滑**回去的，不是跳回去。 */
function lqLeave(e){
  const el = e.currentTarget;
  if(el === _lqEl) _lqEl = null;
  el.style.removeProperty('--mx');
  el.style.removeProperty('--my');
}

/*  给元素挂监听（幂等） */
function lqBindMove(root){
  const scope = root || document.body;
  if(!scope || !scope.querySelectorAll) return;
  let list;
  try{
    list = scope.querySelectorAll(LQ_MOVE_SEL);
  }catch(e){ return; }
  list.forEach(el=>{
    if(el.dataset.lqMove) return;
    el.dataset.lqMove = '1';
    el.addEventListener('pointermove', lqMove, {passive:true});
    el.addEventListener('pointerleave', lqLeave, {passive:true});
  });
}

/*  ── 缩放期间停掉流动 ──
   `html.resizing` 这个类在窗口尺寸变化时会被挂上（见 app.js 后面
   「最大化流畅性」那一段）。这里用 CSS 的动画播放控制来处理，
   不碰 DOM —— 改 style 会让浏览器重算样式，缩放期间正好最怕这个。
   所以真正生效的是 CSS 里的一条规则，这里只负责在
   缩放开始时给流动层打个标记（其实是 CSS 自己判断的，见 app.css：
     html.resizing .lq-eff{animation-play-state:paused}
   ）
   → 这个函数因此**什么都不用做**，留着是为了说明设计意图，
     免得后来的人以为是漏了。 */
/* ══════════ 液态玻璃 v2：启动 ══════════
   首屏先补一遍；之后每次 render() 完成后由 render 尾部再补一遍
   （新任务卡是那时长出来的）。不做 MutationObserver：
   那个太吵，实测在资料页那种一次插几十个节点的地方不划算。 */
(function lqFlowBoot(){
  const go = ()=>{ try{ lqFlow(document.body); }catch(e){} };
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', go);
  }else{
    setTimeout(go, 0);
  }
  /*  双保险：数据回来得晚（首屏要等 get_data），
      那之后再补一次。 */
  setTimeout(go, 900);
  setTimeout(go, 2600);
})();


/* ══════════════ 今日新获得（成绩 / 作业）══════════════
   用户要求：「新增今日新获得成绩作业项目，紧随代办作业之后」。

   ★ 数据从哪来
     `DATA.today_new` —— 后端 `app/today_new.py` 维护的
     **按天累积台账**，不是「跟上一轮比」。

     为什么不跟上一轮比：快照每次抓取都被覆盖，
     中午新出的一条作业到晚上再抓一次就比不出来了。
     详见 app/today_new.py 的模块说明。

   ★ 为什么复用 taskCards()
     视觉上和「全部待办」完全一致，用户不用学新东西。
     连分数都已经由 taskCards 自己渲染成 `.tag.grade` 了
     （形如「A 100 / 100 pts」），所以这里不用再加徽章。

   ★ 为什么空时不渲染
     大部分时候都是空的。空区块会让界面天天挂一块
     「今日无新增」的废话，那正是最烦人的设计。
     所以没有就什么都不画，连 section 都不建。
   ────────────────────────────────────────────────────── */
function todayNewBlock(){
  const tn = (DATA || {}).today_new;
  if(!tn) return '';
  const grades = tn.grades || [];
  const tasks  = tn.tasks  || [];
  if(!grades.length && !tasks.length) return '';

  let inner = '';
  if(grades.length){
    inner += `<div class="tn-group">
      <div class="tn-h"><span class="tn-tag">新成绩</span>
        <span class="tn-n">${grades.length}</span></div>
      ${taskCards(grades, {showMark:false})}
    </div>`;
  }
  if(tasks.length){
    inner += `<div class="tn-group">
      <div class="tn-h"><span class="tn-tag task">新作业</span>
        <span class="tn-n">${tasks.length}</span></div>
      ${taskCards(tasks, {showMark:false})}
    </div>`;
  }

  const total = grades.length + tasks.length;
  return section('todaynew',
    `${icon('i-sparkle','sm inl')}今日新获得`,
    `<span class="cnt">${total}</span>`,
    inner);
}

/* ══════════════ 测试注入点（仅网页版）══════════════
   ★ 为什么需要它

     `DATA` / `lastSig` 这些是顶层 `let`，**不会**变成
     `window.DATA` —— `let` 只活在脚本的词法作用域里。
     所以自动化测试里写 `window.DATA = {...}` 是没用的，
     真正的 DATA 还是 null，render() 第一行就返回了。

     实测踩到过：测试报告「区块顺序 = []」「0/0 张卡」，
     看着像界面坏了，其实是注入没生效。

   ★ 为什么只在网页版挂
     桌面版有 pywebview，数据从后端来。
     这个钩子只在测试页（没有 pywebview）里才有用，
     所以加个判断 —— 正式运行时它根本不会被创建，
     也就不存在「被别的东西误用」的可能。

   ★ 名字带 __ 前缀，不会被任何业务代码碰到。
   ────────────────────────────────────────────────── */
if(!window.pywebview){
  window.__mbTest = {
    /*  塞一份假数据，然后走真实的 render() ——
        这样测的是真渲染路径，不是自己拼一份 HTML。 */
    setData(d){
      DATA = d;
      lastSig = '';
      lastEmptySig = '';
      try{ render(); }catch(e){ return String(e); }
      return null;
    },
    /*  只清签名，便于「同一份数据再渲一次」 */
    resetSig(){ lastSig = ''; lastEmptySig = ''; },
    getData(){ return DATA; }
  };
}
