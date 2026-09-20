/* CampusPulse —— 主逻辑
 * 由 web/split_files.py 从 index.html 内联脚本抽出（2026-09-20）。
 */
const $ = s => document.querySelector(s);
let DATA = null;
let WARM = null;                       /* 预热状态（体现「打开即有内容」） */
let DEFAULT_HOST = '';                 /* 学校网址（引导页预填 + 提示用） */
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
    刷新间隔改成 30 分钟后，显示 "1799s" 很怪，
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

function openURL(u){
  if(!u) return;
  /* 窗口模式（pywebview）→ 调用 Python 用系统浏览器打开 */
  if(window.pywebview?.api?.open_url){ window.pywebview.api.open_url(u); return; }
  /* 网页模式 → 让本地看板服务代为打开（避免被浏览器拦截新窗口） */
  if(location.protocol.startsWith('http')){
    fetch('/api/open?url='+encodeURIComponent(u)).catch(()=>{ window.open(u,'_blank'); });
    return;
  }
  window.open(u,'_blank');
}

/* ══════════ 统一数据/控制通道 ══════════
   窗口模式：经 pywebview 的 api
   网页模式：经本地 HTTP 接口 */
const IS_WEB = location.protocol.startsWith('http');
const IS_WIDGET = !IS_WEB;

function apiGetData(){
  if(window.pywebview?.api?.get_data) return window.pywebview.api.get_data();
  if(IS_WEB) return fetch('/api/data').then(r=>r.json());
  return Promise.resolve(null);
}
function apiRefresh(){
  if(window.pywebview?.api?.refresh) return window.pywebview.api.refresh();
  if(IS_WEB) return fetch('/api/refresh').then(r=>r.json());
}
function apiSetAuto(on){
  if(window.pywebview?.api?.set_auto_refresh) return window.pywebview.api.set_auto_refresh(on);
  if(IS_WEB) return fetch('/api/auto?on='+(on?'1':'0')).then(r=>r.json());
}
/* 预热状态：用于显示「开机已自动更新」徽章 */
function apiWarmStatus(){
  if(window.pywebview?.api?.warmup_status) return window.pywebview.api.warmup_status();
  if(IS_WEB) return fetch('/api/warmup').then(r=>r.json());
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
function apiSaveSetup(login, password, url, suser, spw){
  if(window.pywebview?.api?.save_setup)
    return window.pywebview.api.save_setup(login, password, url, suser, spw);
  return Promise.resolve({ok:false, error:'当前模式不支持保存'});
}

/* ══════════ 统计 ══════════ */
/* ══════════ GPA 页 ══════════
   「单开一页」而不是挤在顶部小卡里 —— 因为查分是高频动作，
   而且信息量大（整体 + 每门课 + 每门课的分类和最近几次）。
    折叠状态存在内存里，切页不丢；但刷新页面会重置（有意的，
     因为每次看分时想关注的课不一样，不想被上次的状态绑住）。 */
let gpaOpen = {};              /* { 课程名: true } */

/* 把一个百分制分数换算成 4.0 估算值（与后端 scraper._est 完全一致） */
function estOf(p){
  if(p == null) return null;
  const bands = [[93,4.0],[90,3.7],[87,3.3],[83,3.0],[80,2.7],[77,2.3],
                 [73,2.0],[70,1.7],[67,1.3],[63,1.0],[60,0.7]];
  for(const [lo,g] of bands) if(p >= lo) return g;
  return 0.0;
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
          : '本机估算：每门课的总评按 90/80/70/60 换算成 4.0，再等权平均。不是学校口径。'}
      </p>
      <p class="gnote-dim">未计入：学分、AP/IB 加权、重修、未出总评的课程。</p>
      <div class="gscale">
        <span><b>93+</b> → 4.0</span><span><b>90+</b> → 3.7</span>
        <span><b>87+</b> → 3.3</span><span><b>83+</b> → 3.0</span>
        <span><b>80+</b> → 2.7</span><span><b>77+</b> → 2.3</span>
        <span><b>73+</b> → 2.0</span><span><b>70+</b> → 1.7</span>
        <span><b>67+</b> → 1.3</span><span><b>63+</b> → 1.0</span>
        <span><b>60+</b> → 0.7</span><span><b>&lt;60</b> → 0</span>
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
function gpaRow(c){
  const p = c.overall_percent;
  const lv = c.overall_level || '';
  const col = p != null ? pctColor(p) : lvlColor(lv);
  const key = c.name || '';
  const isOpen = !!gpaOpen[key];
  const estV = estOf(p);
  const cats = (c.categories || []).filter(x => x.percent != null);
  const graded = (c.tasks || [])
    .filter(t => t.graded && t.score && t.score !== 'N/A').slice(0, 4);

  let h = `<div class="grow${isOpen?' open':''}" data-grow="${esc(key)}"
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
  <div class="gdetail" data-gdet="${esc(key)}"><div class="gdin">`;

  if(estV != null){
    h += `<div class="gdtitle">${icon('i-award','sm inl')}单科估算</div>
      <div style="font-size:11.3px;color:var(--muted);margin-bottom:12px">
        按 ${p.toFixed(1)}% 换算约 <b style="color:var(--text)">${estV.toFixed(2)}</b> / 4.0
        <span style="color:var(--dim)">（本机换算，非学校口径）</span></div>`;
  }

  if(cats.length){
    h += `<div class="gdtitle">${icon('i-list','sm inl')}分类占比</div>`;
    for(const ct of cats.slice(0, 6)){
      const w = Math.max(3, Math.min(100, ct.percent));
      const cc = pctColor(ct.percent);
      h += `<div class="gcat" style="color:${cc}">
        <span class="cn">${esc(ct.name || '未命名分类')}${
          ct.weight ? `<span style="color:var(--dim)"> · 权重 ${esc(String(ct.weight))}%</span>` : ''}</span>
        <span class="cbar"><i style="width:${w}%"></i></span>
        <span class="cv" style="color:${cc}">${ct.percent.toFixed(1)}%</span>
      </div>`;
    }
  }

  if(graded.length){
    h += `<div class="gdtitle" style="margin-top:13px">
      ${icon('i-note','sm inl')}最近成绩</div>`;
    for(const t of graded){
      const tc = t.percent != null ? pctColor(t.percent) : 'var(--text)';
      h += `<div class="gitem">
        <span class="gt">${esc(t.title || '')}</span>
        <span class="gs" style="color:${tc}">${esc(t.score)}</span>
      </div>`;
    }
  }

  if(!cats.length && !graded.length){
    h += `<div class="gempty">这门课还没有可以展开的明细。</div>`;
  }

  h += `<button class="fretry" style="margin-top:12px"
      data-gopen="${esc(c.url || '')}">
    ${icon('i-external','sm')}在 ManageBac 中打开</button>`;

  return h + `</div></div>`;
}

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

    /* 已完成标签：手动标记的单独说明，避免和「已提交」混淆 */
    const doneTag = t.manual
      ? `<span class="tag ok" title="你自己标记的完成，只在本机生效">${icon('i-check','sm')}我已完成</span>`
      : (t.submitted ? `<span class="tag ok">${icon('i-check','sm')}已提交</span>`
         : (t.graded ? `<span class="tag ok">${icon('i-check','sm')}已评分</span>`
            : `<span class="tag ok">${icon('i-check','sm')}已完成</span>`));

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

    return `<div class="card${t.manual?' manual-marked':''}" style="--c:${col}" data-url="${esc(t.url)}">
      ${hasDl?`<button class="dlbtn" title="下载本任务附件"
        data-dl="${esc(cid)}|${esc(tid)}|${esc(t.title)}">${icon('i-download','sm')}</button>`:''}
      ${mkBtn}
      ${t.course?`<div class="cname"><span class="dot"></span>${esc(t.course)}</div>`:''}
      <div class="h">${esc(t.title)}</div>
      <div class="tags">
        ${t.completed ? doneTag : dueTag(d)}
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
      <div class="filesbox" id="fbox-${esc(c.class_id)}"></div>
    </div>
  </div>`;
}

/* ══════════ 图表 ══════════ */
function chartCourses(){
  const cs = (DATA.courses||[]).filter(c=>c.overall_percent!=null);
  if(!cs.length) return `<div class="empty">暂无成绩数据</div>`;
  const rowH=25, W=100, H=cs.length*rowH+6;
  const bars = cs.map((c,i)=>{
    const y=i*rowH+3, w=Math.max(2,(c.overall_percent/100)*W);
    const idx = (DATA.courses||[]).indexOf(c);
    return `<text x="0" y="${y+8}" fill="#8a8171" font-size="8">${esc(c.name.slice(0,19))}</text>
      <rect x="0" y="${y+11}" width="${W}" height="5" rx="2.5" fill="#ede5d6"/>
      <rect x="0" y="${y+11}" width="${w}" height="5" rx="2.5" fill="${colorOf(idx)}">
        <animate attributeName="width" from="0" to="${w}" dur=".6s" fill="freeze"/>
      </rect>
      <text x="${W+2}" y="${y+16}" fill="${pctColor(c.overall_percent)}"
        font-size="8.5" font-weight="700">${c.overall_percent.toFixed(1)}%</text>
      <text x="${W+2}" y="${y+8}" fill="${c.overall_level?lvlColor(c.overall_level):'#a89e8b'}"
        font-size="8" font-weight="700">${esc(c.overall_level||'')}</text>`;
  }).join('');
  return `<div class="cbox">
    <div class="ct">各科总评（网页原始数据）</div>
    <svg viewBox="-1 0 ${W+24} ${H}" width="100%" height="${H*1.4}"
      style="overflow:visible">${bars}</svg>
  </div>`;
}

function chartCatAvg(){
  /* 所有课程的分类平均分横向对比 */
  const map = new Map();
  for(const c of (DATA.courses||[])){
    for(const cat of (c.categories||[])){
      if(cat.name.toLowerCase()==='overall' || cat.percent==null) continue;
      if(!map.has(cat.name)) map.set(cat.name, []);
      map.get(cat.name).push(cat.percent);
    }
  }
  const rows = [...map.entries()]
    .map(([k,v])=>({name:k, avg:v.reduce((a,b)=>a+b,0)/v.length, n:v.length}))
    .sort((a,b)=>b.avg-a.avg);
  if(!rows.length) return '';
  const rowH=21, W=100, H=rows.length*rowH+6;
  const bars = rows.map((r,i)=>{
    const y=i*rowH+3, w=Math.max(2,(r.avg/100)*W);
    return `<text x="0" y="${y+9}" fill="#8a8171" font-size="8">${esc(r.name.slice(0,17))}</text>
      <rect x="0" y="${y+12}" width="${W}" height="4.5" rx="2.2" fill="#ede5d6"/>
      <rect x="0" y="${y+12}" width="${w}" height="4.5" rx="2.2" fill="${pctColor(r.avg)}">
        <animate attributeName="width" from="0" to="${w}" dur=".6s" fill="freeze"/>
      </rect>
      <text x="${W+2}" y="${y+16}" fill="${pctColor(r.avg)}" font-size="8"
        font-weight="700">${r.avg.toFixed(1)}%</text>`;
  }).join('');
  return `<div class="cbox">
    <div class="ct">各分类平均分汇总</div>
    <svg viewBox="-1 0 ${W+24} ${H}" width="100%" height="${H*1.4}"
      style="overflow:overflow">${bars}</svg>
  </div>`;
}

function chartDue(tasks){
  const t = (tasks||[]).filter(x=>x.days_left!=null && x.days_left>=-1);
  if(!t.length) return '';
  const buckets=[0,1,2,3,4,5,6,7];
  const cnt = buckets.map(b=>t.filter(x=> b===7 ? x.days_left>=7 : x.days_left===b).length);
  const mx = Math.max(1,...cnt), W=248, H=58;
  const bw = W/buckets.length;
  const bars = buckets.map((b,i)=>{
    const h=Math.max(2,(cnt[i]/mx)*38), x=i*bw+3, y=42-h;
    const col = b===0?'#f87171': b<=2?'#fb923c': b<=4?'#fbbf24':'#34d399';
    return `<rect x="${x}" y="${y}" width="${bw-6}" height="${h}" rx="3" fill="${col}">
        <animate attributeName="height" from="0" to="${h}" dur=".5s" fill="freeze"/>
        <animate attributeName="y" from="42" to="${y}" dur=".5s" fill="freeze"/>
      </rect>
      ${cnt[i]?`<text x="${x+(bw-6)/2}" y="${y-3}" text-anchor="middle"
        fill="#8a8171" font-size="7.5">${cnt[i]}</text>`:''}
      <text x="${x+(bw-6)/2}" y="53" text-anchor="middle" fill="#a89e8b" font-size="7">
        ${b===7?'7+':b===0?'今天':b+'天'}</text>`;
  }).join('');
  return `<div class="cbox">
    <div class="ct">任务截止分布</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H*1.15}">${bars}</svg>
  </div>`;
}

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
       太低会导致文字看不清（背景透过来太花）。 */
  const k = Math.max(0.45, Math.min(1, o.alpha / 100));
  const f = v => Math.max(0.45, Math.min(1, v * k / (ALPHA_BASE / 100)));
  rt.style.setProperty('--glass',   `rgba(255,253,248,${f(0.72).toFixed(3)})`);
  rt.style.setProperty('--glass-2', `rgba(255,253,248,${f(0.55).toFixed(3)})`);
  rt.style.setProperty('--glass-3', `rgba(255,253,248,${f(0.86).toFixed(3)})`);

  /* ③ 毛玻璃
      关掉时要把背景变成**不透明实色** —— 只去掉 blur 会留一个
       半透明的糊状层，比开着还难看。 */
  if(o.blur){
    rt.style.setProperty('--glassBlur', 'saturate(1.7) blur(22px)');
  }else{
    rt.style.setProperty('--glassBlur', 'none');
    rt.style.setProperty('--glass',   'rgba(255,253,248,0.985)');
    rt.style.setProperty('--glass-2', 'rgba(255,253,248,0.97)');
    rt.style.setProperty('--glass-3', 'rgba(255,253,248,1)');
  }
}

let LOOK = null;

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

  host.classList.add('open');
}

function closeSettings(){
  $('#setpanel')?.classList.remove('open');
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

  clRenderList();
  host.classList.add('open');
  setTimeout(() => { try{ $('#clName')?.focus(); }catch(e){} }, 300);
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
    h += `<div class="clitem">
      <span class="cln">${esc(c.period || '')} ${esc(c.subject || '')}${
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
  const per   = $('#clPeriod')?.value || '';

  if(!name){
    toast('先填个名称', 'err', '');
    try{ $('#clName')?.focus(); }catch(e){}
    return;
  }
  if(!clDays.length){
    toast('选一下星期几', 'err', '');
    return;
  }

  const list = customLessons();
  list.push({
    id: 'c-' + Date.now(),
    weekdays: clDays.slice().sort(),
    period: per,
    subject: name,
    room: room,
    flag: '',
  });
  saveCustomLessons(list);

  /* 清掉输入，方便连着加下一条 */
  try{
    $('#clName').value = '';
    $('#clRoom').value = '';
  }catch(e){}

  clRenderList();
  toast('已添加：' + name, 'ok', '');

  lastSig = '';
  render();
}

function bindCustomPanel(){
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

  /* 回车直接保存 */
  ['#clName', '#clRoom'].forEach(sel => {
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
function section(id, title, countHtml, inner, extra=''){
  const isC = collapsed[id];
  return `<div class="sec ${isC?'collapsed':''}">
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
  try{ return JSON.parse(localStorage.getItem(CL_KEY) || '[]') || []; }
  catch(e){ return []; }
}
function saveCustomLessons(list){
  try{ localStorage.setItem(CL_KEY, JSON.stringify(list || [])); }catch(e){}
}

/* 自编课里，某一天该出现哪些 */
function customForDay(dayISO){
  const wd = new Date(dayISO + 'T00:00:00').getDay();   /* 0=周日 */
  return customLessons().filter(c=>{
    if(c.once) return c.day === dayISO;
    return (c.weekdays || []).indexOf(wd) >= 0;
  }).map(c=>({
    /* 转成和抓取数据一样的结构，后面渲染就不用分叉了 */
    day: dayISO,
    period: c.period || '',
    subject: (c.flag ? '[' + c.flag + '] ' : '') + (c.subject || ''),
    room: c.room || '',
    teacher: '',
    start: '', end: '',
    kind: '自定义',
    custom: true,
  }));
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
  const noP = [];                     /* 没写节次的课（晚自习等） */
  for(const l of dayLessons){
    const key = (l.period || '').trim().toUpperCase();
    if(key && my.some(x => x.period.toUpperCase() === key)){
      /* 同一节次可能有多门（冲突），用数组存 */
      (byP[key] = byP[key] || []).push(l);
    }else{
      noP.push(l);
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
  });
  /* 不在节次表里的（晚自习等）追加在后面，按时间排 */
  noP.sort((a,b)=>String(a.start||'').localeCompare(String(b.start||'')));
  out.push(...noP);
  return out;
}

/* 给整份课表做补全（按天） */
function completeSchedule(lessons, periods){
  const byDay = {};
  for(const l of lessons) (byDay[l.day] = byDay[l.day] || []).push(l);

  /* 自编课也按天塞进去。
      范围只到「抓取数据覆盖的最后一天 + 0」，不要往前铺三周 ——
       否则「每周一上操」会在后续课程区刷出 3 个周一，很吵。
       抓取范围本身是「昨天 ~ 未来 14 天」，跟着它走就够了。 */
  const allDays = new Set(Object.keys(byDay));
  const dayKeys = [...allDays].sort();
  const lastReal = dayKeys.length ? dayKeys[dayKeys.length - 1] : '';

  for(let d = -1; d <= 15; d++){
    const dt = new Date(); dt.setDate(dt.getDate() + d);
    const iso = `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
    /* 超出抓取范围就不展开（除非那天本来就有课） */
    if(lastReal && iso > lastReal && !allDays.has(iso)) continue;
    const extra = customForDay(iso);
    if(extra.length){
      byDay[iso] = (byDay[iso] || []).concat(extra);
      allDays.add(iso);
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
}

/* ══════════ 下载 ══════════ */
function apiDownloadTask(cid, tid, title){
  if(window.pywebview?.api?.download_task)
    return window.pywebview.api.download_task(cid, tid, title);
  if(IS_WEB)
    return fetch(`/api/download_task?cid=${encodeURIComponent(cid)}`
      +`&tid=${encodeURIComponent(tid)}&title=${encodeURIComponent(title)}`)
      .then(r=>r.json()).then(d=>{ showDownloadResult(d, title); return d; });
}
function apiDownloadFiles(cid, name){
  if(window.pywebview?.api?.download_files)
    return window.pywebview.api.download_files(cid, name);
  if(IS_WEB)
    return fetch(`/api/download_files?cid=${encodeURIComponent(cid)}`
      +`&name=${encodeURIComponent(name)}`).then(r=>r.json())
      .then(d=>{ showDownloadResult(d, name); return d; });
}
/* 单个附件直链下载（任务详情面板用） */
function apiDownloadFile(url, filename, subdir){
  if(window.pywebview?.api?.download_file)
    return window.pywebview.api.download_file(url, filename, subdir || '');
  if(IS_WEB)
    return fetch('/api/download_file?url=' + encodeURIComponent(url)
      + '&name=' + encodeURIComponent(filename))
      .then(r=>r.json()).then(d=>{ showDownloadResult(d, filename); return d; });
  return Promise.resolve({ok:false});
}
function apiOpenFolder(path){
  if(window.pywebview?.api?.open_folder)
    return window.pywebview.api.open_folder(path);
  if(IS_WEB) return fetch('/api/open_folder?path='+encodeURIComponent(path));
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
function digestRender(d, curDate){
  const c = d.counts || {};
  const g = d.gpa || {};

  let h = `<div class="dg-head">
    <span class="oi" style="color:var(--blue)">${''}</span>
    <div style="flex:1;min-width:0">
      <div class="t">${icon('i-mail','sm inl')}今日晚报</div>
      <div class="d">${esc(d.date||'')} ${esc(d.weekday||'')} · 生成于 ${esc(d.generated||'')}</div>
    </div>
    <button class="dg-x" id="dgClose" title="关闭">${icon('i-x','sm')}</button>
  </div>
  <div class="dg-body">`;

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
  const hist = (d.history||[]).filter(x=>x.date);
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

  /* 翻看往期 */
  host.querySelectorAll('[data-dgdate]').forEach(b=>{
    b.addEventListener('click', ()=>{
      const d = b.dataset.dgdate;
      if(d === curDate) return;
      apiDigestByDate(d).then(res=>{
        if(res && res.ok !== false){
          res.history = res.history || [];
          const wrap = $('#digwrap');
          if(wrap) wrap.innerHTML = digestRender(res, d);
          bindDigest(d);
        }
      }).catch(()=>{});
    });
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

function refreshDataPanel(){
  apiBackupStatus().then(st=>{
    const wrap = $('#datawrap');
    if(wrap && $('#datapanel')?.classList.contains('open')){
      wrap.innerHTML = dataRender(st);
      bindData();
    }
  }).catch(()=>{});
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
| '程序正在自动重新登录，稍等一会儿就好。';
    btn = `<button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}立即重新登录</button>`;
  }else if(st === 'error'){
    iconName = 'i-alert';
    title = '读取失败';
    desc = DATA.message || '请检查网络，然后点右上角刷新重试。';
    btn = `<button class="es-btn" data-relogin="1">
      ${icon('i-refresh','sm')}重试</button>`;
  }else if(st === 'running'){
    iconName = 'i-hourglass';
    title = '正在读取数据……';
    desc = '正在登录并读取所有课程，约 20 秒。';
  }else{
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
  /* 课表页不可见时跳过（省 CPU） */
  const pg = document.querySelector('#page-sch');
  if(pg && pg.offsetParent === null) return;
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

function render(){
  if(!DATA) return;

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
  if(DATA.auto_refresh){
    const auto = cd>0 ? `· ${fmtCountdown(cd)}后自动刷新` : '· 正在刷新…';
    mtext = mtext ? `${mtext}  ${auto}` : auto;
  }
  const mcls = mtext
    ? 'show ' + (DATA.status==='need_login'||DATA.status==='running'
        ? 'warn' : DATA.status==='error' ? 'err' : '')
    : '';
  if(msg.textContent !== mtext) msg.textContent = mtext;
  if(msg.className !== mcls) msg.className = mcls;

  const updTxt = DATA.updated || '未更新';
  const updEl = $('#upd');
  if(updEl.textContent !== updTxt) updEl.textContent = updTxt;
  $('#btnRefresh').classList.toggle('spin', DATA.status==='running');

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
      ? `每 ${(DATA.interval_text || '30 分钟')}自动刷新，还剩 ${fmtCountdown(cd)}`
      : '自动刷新已暂停';
  }
  const ba = $('#btnAuto');
  if(ba){
    /*  不能整个刷 textContent —— 那样会把里面的 <svg> 图标抹掉。
       只需要换 <use> 的 href 即可。 */
    const u = ba.querySelector('use');
    if(u) u.setAttribute('href', DATA.auto_refresh ? '#i-pause' : '#i-play');
    ba.classList.toggle('paused', !DATA.auto_refresh);
    ba.title = DATA.auto_refresh ? '暂停自动刷新' : '开启自动刷新';
  }

  if(!tasks.length && !courses.length){
    /* 没有任何数据 → 显示明确的原因与操作指引，绝不空白 */
    $('#page-grade').innerHTML = emptyState();
    $('#page-sch').innerHTML = DATA.schedule && DATA.schedule.ready
      ? schedulePage() : scheduleEmptyState();

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
    $('#page-grade').innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
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
  const sig = JSON.stringify([
    pendingAll.map(t=>t.url + '|' + t.days_left), done.length,
    DATA.updated, DATA.status, collapsed, openCourses,
    (DATA.schedule||{}).updated, scheduleDirty
  ]);
  if(sig === lastSig) return;
  lastSig = sig;
  scheduleDirty = false;

  /* ── 第 1 页：课程成绩（待办 / 测验 / 作业分类） ── */
  let h = '';

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

  /* ── 第 2 页：今日课表 ── */
  $('#page-sch').innerHTML = schedulePage();

  /* ── 第 3 页：资料（实时浏览 ManageBac Files） ── */
  if(FSTATE.cid){
    /* 数据变了 → 重绘（保留当前所在文件夹） */
    $('#page-files').innerHTML = filesPage();
    bindFiles();
  }else{
    /* 首次进入：选第一门课并拉取 */
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

  bind();
  /*  空状态里的「修复」按钮也要绑定（它们不在 bind() 的扫描范围内） */
  bindRelogin(document);
}

/* ══════════ 「我已完成，不再提示」 ══════════
   用途：线下交了纸质版、老师没给成绩、线上也没有提交徽章的任务。
        按唯一完成规则会一直挂在待办里碍眼，这里允许手动标记。
    只存本机，不会写入 ManageBac，不影响学校系统。 */

/* 找到本地缓存的任务对象（用于取 title / course） */
function findTaskByUrl(url){
  const all = [...(DATA?.tasks||[])];
  (DATA?.courses||[]).forEach(c => (c.tasks||[]).forEach(t => all.push(t)));
  return all.find(t => t.url === url) || null;
}

function apiMarkDone(url, title, course){
  if(window.pywebview?.api?.mark_done)
    return window.pywebview.api.mark_done(url, title, course);
  if(IS_WEB)
    return fetch('/api/mark_done?url=' + encodeURIComponent(url)
      + '&title=' + encodeURIComponent(title)
      + '&course=' + encodeURIComponent(course)).then(r=>r.json());
  /* 纯静态预览：只改内存，刷新即失效 */
  return Promise.resolve({ok:true, local:true});
}
function apiUnmarkDone(url, title, course){
  if(window.pywebview?.api?.unmark_done)
    return window.pywebview.api.unmark_done(url, title, course);
  if(IS_WEB)
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
  if(IS_WEB)
    return fetch('/api/files_list?cid=' + encodeURIComponent(cid)
      + '&fid=' + encodeURIComponent(fid || '')
      + (force ? '&force=1' : '')).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持',
                          folders:[], files:[], items:[]});
}

function apiFilesDownload(item){
  if(window.pywebview?.api?.files_download)
    return window.pywebview.api.files_download(item);
  if(IS_WEB)
    return fetch('/api/files_download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(item)
    }).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

function apiFilesDownloadAll(cid, fid, className, folderName){
  if(window.pywebview?.api?.files_download_all)
    return window.pywebview.api.files_download_all(cid, fid || '',
                                                   className || '',
                                                   folderName || '');
  if(IS_WEB)
    return fetch('/api/files_download_all?cid=' + encodeURIComponent(cid)
      + '&fid=' + encodeURIComponent(fid || '')
      + '&cls=' + encodeURIComponent(className || '')
      + '&folder=' + encodeURIComponent(folderName || '')).then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持'});
}

function apiFilesOpen(classId){
  if(window.pywebview?.api?.open_url && classId){
    const c = (DATA?.courses||[]).find(x => x.class_id === classId);
    openURL(c ? (c.url + '/files') : '');
  }
  return Promise.resolve();
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

function apiTaskDetail(url){
  if(window.pywebview?.api?.task_detail)
    return window.pywebview.api.task_detail(url, false);
  if(IS_WEB)
    return fetch('/api/task_detail?url=' + encodeURIComponent(url))
      .then(r=>r.json());
  return Promise.resolve({ok:false, error:'当前模式不支持', attachments:[],
                          description:'', grade:{}});
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
function setSpin(el, on){
  if(!el) return;
  el.innerHTML = on ? icon('i-refresh') : el.dataset.rest;
  el.classList.toggle('spin', !!on);
}

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
function dpLoading(){
  return `<div class="dpanel"><div class="dp-load">
    <span class="spin-ic">${icon('i-refresh')}</span>正在读取详情……</div></div>`;
}

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

  apiTaskDetail(url).then(d=>{
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

/* 构建引导页（图标 + 眼睛 + 按钮文案都用 icon()，保持一致） */
function buildSetup(){
  const ic = $('#suIc'); if(ic) ic.innerHTML = icon('i-shield','lg');
  const e1 = $('#suEye'); if(e1) e1.innerHTML = icon('i-eye','sm');
  const f1 = $('#suFic'); if(f1) f1.innerHTML = icon('i-cal','sm inl');
  const g1 = $('#suGo');  if(g1) g1.innerHTML = icon('i-check','sm') + '<span>登录</span>';
}

function openSetup(){
  const el = $('#setup');
  if(!el) return;
  el.classList.add('open');
  document.body.classList.add('setting-up');
  /* 网址默认值：从后端拿到的默认站点里抽域名 */
  const u = $('#suUrl');
  if(u && !u.value){
    u.value = suHostOf(DEFAULT_HOST || 'your-school.managebac.cn');
  }
  setTimeout(()=>{ try{ $('#suUser')?.focus(); }catch(e){} }, 320);
}
function closeSetup(){
  const el = $('#setup');
  if(el) el.classList.remove('open');
  document.body.classList.remove('setting-up');
}

/* 判断是否需要引导页，需要就打开。
    抽成函数（不直接写在 DOMContentLoaded 里）的理由：
     预览页 / pywebview 可能在 DOMContentLoaded **之后**才注入脚本，
     那种情况下监听器永远不会触发 —— 这是在浏览器里验证时真实踩到的坑。 */
async function bootSetup(){
  try{
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

/* 点击「测试并保存」 */
async function suSubmit(){
  if(suBusy) return;

  const host = suHostOf($('#suUrl')?.value);
  const user = ($('#suUser')?.value || '').trim();
  const pw   = ($('#suPw')?.value || '');
  const suser = ($('#suSUser')?.value || '').trim();
  const spw   = ($('#suSPw')?.value || '');

  /* ── 本地校验（不发请求就能发现的问题先挡掉）── */
  ['#suUrl','#suUser','#suPw'].forEach(s=>{
    const el = $(s); if(el) el.classList.remove('bad');
  });
  if(!host){
    $('#suUrl')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填学校网址');
    return;
  }
  if(!user){
    $('#suUser')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填 ManageBac 账号');
    return;
  }
  if(!pw){
    $('#suPw')?.classList.add('bad');
    suMsg('err', icon('i-alert','sm inl') + '请填 ManageBac 密码');
    return;
  }

  suBusy = true;
  const btn = $('#suGo');
  const oldHtml = btn ? btn.innerHTML : '';
  if(btn){
    btn.disabled = true;
    btn.innerHTML = icon('i-refresh','sm spin') + '<span>正在测试登录…</span>';
  }
  suMsg('info', icon('i-refresh','sm inl')
    + '正在连接学校系统，大约 10 秒。请别关窗口。');

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
    suMsg('info', icon('i-check','sm inl') + '登录成功，正在保存…');
    const s = await apiSaveSetup(user, pw, fullUrl, suser, spw);
    if(!s || !s.ok){
      suMsg('err', icon('i-alert','sm inl')
        + esc((s && s.error) || '保存失败'));
      return;
    }

    suMsg('ok', icon('i-check','sm inl')
      + '全部搞定！正在读取你的作业和成绩…');

    /* ── 第三步：进入主界面并立刻抓一次 ── */
    HAS_CREDENTIALS = true;      /*  填完了，之后不再显示引导 */
    setTimeout(()=>{ closeSetup(); }, 700);
    lastSig = '';
    poll();
    try{ await apiRefresh(); }catch(e){}
    /* 抓取是后台跑的，前端每秒轮询自己会更新，不用等在这 */
    HAS_CREDENTIALS = true;          /*  填完了，之后不再显示引导 */

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
  buildSetup();

  $('#suEye')?.addEventListener('click', e=>{
    e.stopPropagation();
    const p = $('#suPw');
    if(!p) return;
    const show = p.type === 'password';
    p.type = show ? 'text' : 'password';
    $('#suEye').innerHTML = icon(show ? 'i-eye-off' : 'i-eye', 'sm');
  });

  $('#suFhead')?.addEventListener('click', ()=>{
    $('#suFold')?.classList.toggle('open');
  });

  $('#suGo')?.addEventListener('click', e=>{
    e.stopPropagation();
    suSubmit();
  });

  /* 回车提交（三个框都支持） */
  ['#suUrl','#suUser','#suPw','#suSUser','#suSPw'].forEach(s=>{
    const el = $(s);
    if(!el) return;
    el.addEventListener('keydown', e=>{
      if(e.key === 'Enter'){
        e.preventDefault();
        suSubmit();
      }
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

  /* 资料（Files）按钮 */
  document.querySelectorAll('.fbtn[data-files]').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.stopPropagation();
      const [cid, name] = btn.dataset.files.split('|');
      toggleFiles(cid, name, '');
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
function apiListFiles(cid, folder){
  if(window.pywebview?.api?.list_files)
    return window.pywebview.api.list_files(cid, folder || '');
  return Promise.resolve({ok:false, items:[]});
}

function renderFiles(box, cid, name, folder, items){
  if(!items.length){
    box.innerHTML = `<div class="empty" style="padding:9px">没有文件</div>`;
    return;
  }
  const back = folder
    ? `<div class="fitem" data-back="1"><span class="fi">${icon('i-back','sm')}</span>
         <span class="fn">返回上一级</span></div>` : '';
  box.innerHTML = back + items.map(it=>{
    if(it.kind === 'folder'){
      return `<div class="fitem folder" data-dir="${esc(it.folderId)}">
        <span class="fi">${icon('i-folder','sm')}</span>
        <span class="fn">${esc(it.name)}</span></div>`;
    }
    return `<div class="fitem" data-file="${esc(it.url)}"
        data-name="${esc(it.name)}">
      <span class="fi">${icon(fileIcon(it.name),'sm')}</span>
      <span class="fn">${esc(it.name)}</span>
      ${it.size?`<span class="fs">${esc(it.size)}</span>`:''}</div>`;
  }).join('');

  box.querySelectorAll('[data-dir]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      toggleFiles(cid, name, el.dataset.dir);
    });
  });
  box.querySelectorAll('[data-back]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      toggleFiles(cid, name, '');
    });
  });
  box.querySelectorAll('[data-file]').forEach(el=>{
    el.addEventListener('click', e=>{
      e.stopPropagation();
      const url = el.dataset.file, fn = el.dataset.name;
      toast('正在下载 ' + fn + ' ……', '', '');
      Promise.resolve(apiDownloadOne(url, fn, name));
    });
  });
}

/* 文件名 → 图标 id（旧的卡片内浏览器也用这个） */
function fileIcon(name){
  const n = (name||'').toLowerCase();
  if(/\.pdf$/.test(n)) return 'i-file';
  if(/\.docx?$/.test(n)) return 'i-doc';
  if(/\.pptx?$/.test(n)) return 'i-slides';
  if(/\.(xlsx?|csv)$/.test(n)) return 'i-table';
  if(/\.(zip|rar|7z)$/.test(n)) return 'i-zip';
  if(/\.(png|jpe?g|gif|webp|bmp)$/.test(n)) return 'i-image';
  if(/\.(mp4|mov|avi)$/.test(n)) return 'i-video';
  if(/\.(mp3|wav|m4a)$/.test(n)) return 'i-audio';
  return 'i-file';
}

let filesCache = {};
function toggleFiles(cid, name, folder){
  const box = document.getElementById('fbox-' + cid);
  if(!box) return;
  const key = cid + '|' + (folder||'');
  if(box.dataset.key === key && box.innerHTML.trim()){
    box.innerHTML = '';                 // 收起
    box.dataset.key = '';
    return;
  }
  box.dataset.key = key;
  box.innerHTML = `<div class="empty" style="padding:9px">正在读取……</div>`;
  if(filesCache[key]){
    renderFiles(box, cid, name, folder, filesCache[key]);
    return;
  }
  apiListFiles(cid, folder).then(d=>{
    const items = (d && d.items) || [];
    filesCache[key] = items;
    renderFiles(box, cid, name, folder, items);
  }).catch(()=>{
    box.innerHTML = `<div class="empty" style="padding:9px">读取失败</div>`;
  });
}

function apiDownloadOne(url, filename, subdir){
  if(window.pywebview?.api?.download_file)
    return window.pywebview.api.download_file(url, filename, subdir);
}

/* ══════════ 按钮 ══════════ */
$('#btnRefresh').addEventListener('click',()=>{
  $('#btnRefresh').classList.add('spin');
  apiRefresh();
});
$('#btnAuto').addEventListener('click',()=>{
  const on = !(DATA && DATA.auto_refresh);
  apiSetAuto(on);
  if(DATA) DATA.auto_refresh = on;
  render();
});
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
  if(IS_WEB){
    const b = document.getElementById('btnMin');
    if(b) b.style.display = 'none';
  }

  /*  首次运行：看有没有填过账号。
     没填过 → 弹出引导页（盖住主界面）；
     填过   → 什么都不做，直接进主界面。 */
  bindSetup();
  bootSetup();
  bindCustomPanel();
  bindSettings();

  /* Tab 点击 */
  document.querySelectorAll('.tab').forEach(el=>{
    el.addEventListener('click', ()=> switchTab(+el.dataset.tab));
  });

  /* 左右滑动切换（共 4 页） */
  const MAXTAB = 3;
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

    /* 鼠标横向滚轮 / Shift+滚轮也可切换 */
    vp.addEventListener('wheel', e=>{
      if(Math.abs(e.deltaX) > Math.abs(e.deltaY) + 8){
        if(e.deltaX > 24 && curTab < MAXTAB) switchTab(curTab + 1);
        else if(e.deltaX < -24 && curTab > 0) switchTab(curTab - 1);
      }
    }, {passive:true});
  }

  /* 键盘左右方向键 */
  document.addEventListener('keydown', e=>{
    /* 输入框里按方向键不切页 */
    const t = e.target;
    if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
| t.tagName === 'SELECT')) return;
    if(e.key === 'ArrowRight' && curTab < 3) switchTab(curTab + 1);
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

$('#btnLogin').addEventListener('click',()=>{
  /*  点顶栏钥匙：
       没填过账号 → 打开引导页让他填
       填过了     → 重新登录（不是弹提示让你敲命令） */
  if(!HAS_CREDENTIALS){ openSetup(); return; }

  const b = $('#btnLogin');
  if(b && b.classList.contains('spin')) return;    /* 防重复点 */

  if(b){ b.classList.add('spin'); b.title = '正在登录…'; }
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
    setTimeout(()=>{
      if(b){ b.classList.remove('spin'); b.title = '重新登录（自动）'; }
    }, 4000);
  });
});
/* ── 晚报：点顶部信封按钮展开 ── */
$('#btnDigest').addEventListener('click', openDigest);
/* ── 数据与备份 ── */
$('#btnData').addEventListener('click', openData);

/* ESC 关闭面板（晚报 / 数据与备份）—— 优先于其他操作 */
document.addEventListener('keydown', e=>{
  if(e.key !== 'Escape') return;
  const dg = $('#digest');
  const dp = $('#datapanel');
  if(dg && dg.classList.contains('open')){
    e.stopPropagation();
    closeDigest();
    return;
  }
  if(dp && dp.classList.contains('open')){
    e.stopPropagation();
    closeData();
  }
}, true);
$('#btnMin').addEventListener('click',()=>{
  if(window.pywebview?.api?.minimize) window.pywebview.api.minimize();
  else document.body.classList.toggle('compact');
});
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
  /* 网页模式下隐藏「最小化」按钮（那是桌面窗口才有的功能） */
  if(IS_WEB){
    const b = document.getElementById('btnMin');
    if(b) b.style.display = 'none';
  }

  /*  首次运行：看有没有填过账号。
     没填过 → 弹出引导页（盖住主界面）；
     填过   → 什么都不做，直接进主界面。 */
  bindSetup();
  bootSetup();

  /* Tab 点击 */
  document.querySelectorAll('.tab').forEach(el=>{
    el.addEventListener('click', ()=> switchTab(+el.dataset.tab));
  });

  /* 左右滑动切换（共 4 页） */
  const MAXTAB = 3;
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

    /* 鼠标横向滚轮 / Shift+滚轮也可切换 */
    vp.addEventListener('wheel', e=>{
      if(Math.abs(e.deltaX) > Math.abs(e.deltaY) + 8){
        if(e.deltaX > 24 && curTab < MAXTAB) switchTab(curTab + 1);
        else if(e.deltaX < -24 && curTab > 0) switchTab(curTab - 1);
      }
    }, {passive:true});
  }

  /* 键盘左右方向键 */
  document.addEventListener('keydown', e=>{
    /* 输入框里按方向键不切页 */
    const t = e.target;
    if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
| t.tagName === 'SELECT')) return;
    if(e.key === 'ArrowRight' && curTab < 3) switchTab(curTab + 1);
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
