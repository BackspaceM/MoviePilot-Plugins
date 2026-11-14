import html
from typing import Any, Dict, List

_STYLE = """
<style>
.pdg{color:inherit;font-family:inherit;line-height:1.5;font-size:14px}
.pdg *{box-sizing:border-box}
.pdg-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:2px 0 16px}
.pdg-logo{width:42px;height:42px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#4F46E5,#7C3AED);box-shadow:0 6px 18px rgba(99,102,241,.32)}
.pdg-logo svg{width:23px;height:23px}
.pdg-title{font-size:19px;font-weight:700;letter-spacing:.3px}
.pdg-sub{font-size:12px;opacity:.62;margin-top:1px}
.pdg-sec{display:flex;align-items:baseline;gap:8px;font-size:13px;font-weight:650;margin:16px 0 9px}
.pdg-sec:before{content:'';width:3px;height:13px;border-radius:3px;background:linear-gradient(135deg,#4F46E5,#A855F7)}
.pdg-card{background:rgba(127,127,127,.075);border:1px solid rgba(127,127,127,.16);
  border-radius:14px;padding:12px 15px}
.pdg-stat{display:flex;align-items:baseline;gap:10px;padding:7px 0;border-bottom:1px solid rgba(127,127,127,.12);font-size:12px}
.pdg-stat:last-child{border-bottom:0}
.pdg-stat-label{opacity:.6;min-width:88px;white-space:nowrap}
.pdg-stat-hint{opacity:.45;font-size:11px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pdg-stat-val{font-weight:700;font-size:14px;white-space:nowrap;
  background:linear-gradient(135deg,#6366F1,#A855F7);-webkit-background-clip:text;background-clip:text;color:transparent}
.pdg-upload{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  border:1px dashed rgba(127,127,127,.34);border-radius:12px;padding:13px 15px}
.pdg-upload input{font-size:12px;color:inherit;max-width:100%}
.pdg-tip{font-size:12px;opacity:.7;flex:1;min-width:180px}
.pdg-note{font-size:12px;opacity:.68;line-height:1.8;padding:10px 13px;border-radius:10px;
  background:rgba(127,127,127,.06);border:1px solid rgba(127,127,127,.13)}
.pdg-note b{font-weight:700}
.pdg-log{font-size:11px;opacity:.6;line-height:1.9;white-space:pre-wrap;word-break:break-all;margin:0}
.pdg-foot{font-size:11px;opacity:.46;margin-top:14px;line-height:1.7}
@media (max-width:620px){.pdg-stat{flex-wrap:wrap}.pdg-stat-hint{display:none}}
</style>
"""

_LOGO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 3v11"/><path d="M8 7l4-4 4 4"/>'
    '<path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>'
    '<path d="M8 12h8"/>'
    '</svg>'
)

_UPLOAD_JS = (
    "var tip=document.getElementById('pdgTip');"
    "var f=this.files?this.files[0]:null;"
    "if(!f){return;}"
    "tip.textContent='正在上传 '+f.name+' ...';"
    "var rd=new FileReader();"
    "rd.onload=function(){"
    "var s=String(rd.result||'');"
    "var k=s.indexOf(',');"
    "var body=k>=0?s.slice(k+1):s;"
    "window.MoviePilotAPI.post('plugin/PluginDebugger/upload',{name:f.name,data:body})"
    ".then(function(x){"
    "var m=x&&x.message?x.message:(x&&x.data&&x.data.message?x.data.message:'上传完成，点一下刷新');"
    "tip.textContent=m;"
    "})"
    ".catch(function(e){tip.textContent='上传失败，看看插件日志'});"
    "};"
    "rd.readAsDataURL(f);"
)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _stat(label: str, hint: str, value: Any) -> str:
    return (
        '<div class="pdg-stat">'
        f'<span class="pdg-stat-label">{_e(label)}</span>'
        f'<span class="pdg-stat-hint">{_e(hint)}</span>'
        f'<span class="pdg-stat-val">{_e(value)}</span>'
        '</div>'
    )


def render(data: Dict[str, Any]) -> str:
    state = data if isinstance(data, dict) else {}
    uploads = state.get("uploads") if isinstance(state.get("uploads"), list) else []
    history = state.get("history") if isinstance(state.get("history"), list) else []
    log = state.get("log") if isinstance(state.get("log"), list) else []

    stats = "".join([
        _stat("已上传", "ZIP 包数量", f"{len(uploads)} 个"),
        _stat("已安装", "累计成功安装", f"{int(state.get('install_count') or 0)} 次"),
        _stat("最近一次", state.get("last_install_at") or "还没有装过", state.get("last_install_id") or "—"),
        _stat("插件目录", "解压后放到这里", state.get("plugins_root") or "读不到"),
        _stat("上传上限", "设置里可改", state.get("max_size_text") or "—"),
    ])

    upload_block = (
        '<div class="pdg-sec">上传 ZIP</div>'
        '<div class="pdg-upload">'
        f'<input type="file" accept=".zip,application/zip" onchange="{_e(_UPLOAD_JS)}">'
        '<span class="pdg-tip" id="pdgTip">选一个插件 ZIP，上传完点「刷新状态」就能看到，再点「安装」。</span>'
        '</div>'
    )

    notes = (
        '<div class="pdg-sec">说明</div>'
        '<div class="pdg-note">'
        'ZIP 里要有一个插件目录，目录里放 <b>__init__.py</b>（主类继承 _PluginBase）；'
        '整个仓库的 ZIP 也行，会自动挑最浅的那个插件目录，包里有多个时可以点「安装」后在设置里指定目录。<br>'
        '也可以不走上传：把 ZIP 放到 MoviePilot 容器里能看到的路径（比如 NAS 映射目录），'
        '在「安装」时填路径。<br>'
        '安装会先备份旧版本到插件数据目录，再解压到插件目录并尝试自动重载；'
        '如果自动重载没成功，文件已经放好了，去插件管理页手动重载一次即可。<br>'
        '带 pyproject.toml / requirements.txt 的插件不会在这里装依赖，'
        '避免依赖装不上把整个插件回滚，需要时重启 MoviePilot 让宿主安装。'
        '</div>'
    )

    lines = [str(x) for x in log[-8:] if isinstance(x, str)]
    log_block = ""
    if lines:
        log_block = ('<div class="pdg-sec">最近操作</div><div class="pdg-card">'
                     f'<pre class="pdg-log">{_e(chr(10).join(lines))}</pre></div>')

    history_block = ""
    if history:
        rows = []
        for row in history[:8]:
            if not isinstance(row, dict):
                continue
            rows.append(_stat(str(row.get("time") or "—"),
                              f"{row.get('plugin_id') or '—'} · {row.get('version') or '—'} · {row.get('file') or '—'}",
                              row.get("result") or "—"))
        history_block = '<div class="pdg-sec">安装记录</div><div class="pdg-card">' + "".join(rows) + "</div>"

    foot = ('<div class="pdg-foot">页面打开时只读一次状态，不轮询。上传的文件只存在插件数据目录，'
            '删除按钮要两步确认，确认按钮就在同一行。安装会动插件目录，测试环境用，别在正式库里乱装来源不明的包。</div>')

    return (
        '<div class="pdg">' + _STYLE +
        '<div class="pdg-head"><div class="pdg-logo">' + _LOGO + '</div>'
        '<div><div class="pdg-title">插件调试</div>'
        '<div class="pdg-sub">ZIP 直接装进 MoviePilot，改完代码打个包就能测</div></div></div>'
        '<div class="pdg-card">' + stats + '</div>'
        + upload_block + history_block + notes + log_block + foot + '</div>'
    )
