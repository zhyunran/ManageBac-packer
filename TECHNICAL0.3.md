# ManageBac 解析规则（基于真实 HTML 取证）

> 所有选择器均来自 `data/probe_out/*.html` 实测快照，**不是猜测**。
> 若页面改版导致失效，重新运行 `uv run probe_dates.py` 取证。

---

## 一、任务来源（两个页面，按 ID 合并）

| 来源 | URL | 特点 |
|---|---|---|
| 课程主页 | `/student/classes/{cid}` | 含"近期任务"卡片；**混有周历表格** |
| All Tasks | `/student/classes/{cid}/core_tasks` | **完整列表**，标题干净、状态完整 |

**必须合并**：主页有近期新任务，`/core_tasks` 有完整历史。
按 `/core_tasks/(\d+)` 里的 ID 去重。

[注意] **周历表格陷阱**：`table.f-tw-calendar` 里的链接标题带时间前缀
（如 `8:50 AM The Iron Man - Test`），且**日期在表头列、不在任务内**。
处理：这类链接只取标题，用 `clean_title()` 去掉时间前缀；日期交给 `/core_tasks`。

---

## 二、任务卡片结构

```html
<div class="fusion-card-item short-assignment section hstack flex-wrap">
  <div class="date-badge">                <!-- 日期徽章 -->
    <div class="month">Jan</div>
    <div class="day">17</div>
  </div>
  <div class="h4 title">
    <a href="/student/classes/{cid}/core_tasks/{tid}">任务标题</a>
  </div>
  <div class="labels-set">                <!-- 分类标签 -->
    <div class="label">Formative</div>
    <div class="label">Homework</div>

    <!--  提交状态徽章 -->
    <span class="badge color-box-green" data-bs-title="3 days early">
      <svg class="fi fi-check_circle_fill"></svg>
      <span class="badge-label">Submitted</span>
    </span>
  </div>
  <span class="due-date">
    <div class="due regular">Friday at 8:00 AM</div>   <!-- 只有星期，无年份 -->
  </span>
</div>

<!-- 成绩单元格：任务卡片的兄弟节点 -->
<div class="assessment task-score assessment-cell">
  <div class="label label-not-applicable">N/A</div>   <!-- 或 "B (85.46%)" -->
</div>
```

---

## 三、 提交状态（核心修正）

**位置**：`div.labels-set > span.badge > span.badge-label`

实测到的取值与判定：

| badge class | `data-bs-title` | `badge-label` | 含义 | 判定 |
|---|---|---|---|---|
| `color-box-green` | `3 days early` | `Submitted` | 已提交（提前 3 天） | [OK] 已完成 |
| `color-box-green` | `16 hours early` | `Submitted` | 已提交（提前 16 小时） | [OK] 已完成 |
| `color-box-gray` | `Waiting` | `Pending` | 待提交 |  未完成 |

**辅助证据**（`div.assessment-cell` 内）：

| cell class | 文本 | 含义 |
|---|---|---|
| `not-submitted` | `Not Submitted` | 未提交 |
| `not-assessed` | — | 未评分 |

**判定规则**：
> 完成 = **已提交文件** 或 **老师已给分**

```
completed = (badge_label == "Submitted") or (score is not None)
```

[注意] **不要**用 `days_left` 判断完成状态。
[注意] 常见误判：化学海报（`暑假作业-化学海报`）实际是 `Submitted`，
但旧逻辑因为没解析 badge，把它当成"即将截止"重复提醒。

---

## 四、 截止日期（跨年修正）

页面**只有** `Jan 17`（月+日）和 `Sunday at 9:15 AM`（星期+时间），
**都没有年份**。

**旧逻辑的 bug**：`resolve_year()` 只取「今天前后 180 天」内的年份。
对 `Jan 17`：今天 2026-09-17，候选年 2026 的 Jan 17 已过去 243 天（超出窗口），
2027 又超出 → 回落到当年 2026 → 得到已过去的日期 → 被当成过期。

**正确做法**：日期是**期限**，通常指向**未来或最近的过去**。
优先选择「最近的未来日期」；只有所有未来都超出合理范围时，才回退到过去。

```
候选年顺序：今年 → 明年 → 后年 → 去年
取第一个使 (候选日期 - 今天) 在 [0, 400] 天内的年份
若都不满足，取「绝对值最小」的那个
```

对 `Jan 17`（今天 2026-09-17）→ 正确结果 **2027-01-17**（约 122 天后）。

**星期文本**要与徽章日期**交叉验证**：
`Sunday at 9:15 AM` + `Jan 17` → 2027-01-17 确实是星期日 [OK]

---

## 五、分类成绩（`/units` 页）

```html
<section>
  <h6>Task Category Averages</h6>
  <div class="sidebar-items-list">
    <div class="list-item list-item-head">
      <div class="cell">Category (Weight)</div>
      <div class="cell">Mark (Score)</div>
    </div>
    <div class="list-item">
      <div class="cell"><strong>Overall</strong></div>
      <div class="cell"><strong>B</strong> (85.46%)</div>
    </div>
    <div class="list-item">
      <div class="cell">Quiz (20%)</div>
      <div class="cell"><strong>C</strong> (76.67%)</div>
    </div>
  </div>
</section>
```

- 分类名与权重：`Quiz (20%)` → 正则 `^(.*?)\s*\(\s*([\d.]+)\s*%\s*\)$`
- 等级与得分：`B (85.46%)` → 正则 `^([A-F][+-]?)\s*\(\s*([\d.]+)\s*%\s*\)$`
- 未出分显示 `-`
- 总评 = 名为 `Overall` 的那一行

**任务完成统计**（同页）：`Overall Task Completion ... 3 Submitted - 2 Late - 0 Pending`

---

## 六、容易踩的坑

1. **`/tasks_and_units` 是 404** —— 正确路径是 **`/units`**
2. **`/student/tasks` 是 404** —— 没有全局任务页，必须逐课程汇总
3. **JSON 接口返回 406** —— 站点拒绝 JSON，只能解析 HTML
4. **周期表格**里的任务标题带时间前缀，必须清理
5. **日期无年份** —— 必须按「期限指向未来」推断
6. **判定完成不能只看日期** —— 要用 badge 的 `Submitted`
7. `/core_tasks` 列表**不含** `date-badge`（只有主页卡片有），
   但**含**提交状态徽章 —— 两者互补，必须合并

---

## 七、复现命令

```powershell
uv run connect.py            # 登录（首次）
uv run probe_dates.py        # 抓取证快照到 data/probe_out/
uv run connect.py --probe    # 抓取并打印全部明细
uv run widget.py             # 桌面小组件
uv run dashboard.py          # 网页版看板
```
