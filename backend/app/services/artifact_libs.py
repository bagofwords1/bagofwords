"""Helper for loading vendored JS libraries for artifact rendering in headless browser.

In airgapped deployments, CDN URLs are not available. This module reads the
vendored JS files from disk and returns them as inline <script> tags for use
with Playwright's page.set_content() (which renders at about:blank and cannot
resolve relative paths).
"""

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths where vendored libs may be found (checked in order):
# 1. Nuxt build output (production Docker image)
# 2. Frontend public dir (local development / Docker with public copied)
_CANDIDATE_DIRS = [
    Path(__file__).parent.parent.parent.parent / "frontend" / ".output" / "public" / "libs",
    Path(__file__).parent.parent.parent.parent / "frontend" / "public" / "libs",
]

# Libraries needed for dashboard (page) mode artifacts
_PAGE_LIBS = [
    "tailwindcss-3.4.16.js",
    "react-18.development.js",
    "react-dom-18.development.js",
    "babel-standalone.min.js",
    "echarts-5.min.js",
    "react-is-18.production.min.js",
    "recharts-3.8.1.min.js",
]

# Libraries needed for slides mode artifacts
_SLIDES_LIBS = [
    "tailwindcss-3.4.16.js",
]


_PAGE_GLOBALS = """
if(window.Recharts)Object.assign(window,Recharts);
window.useState=React.useState;
window.useEffect=React.useEffect;
window.useRef=React.useRef;
window.useMemo=React.useMemo;
window.useCallback=React.useCallback;
echarts.registerTheme('bow',{color:['#3B82F6','#10B981','#8B5CF6','#F59E0B','#EF4444','#06B6D4','#EC4899','#14B8A6','#60A5FA','#34D399'],backgroundColor:'transparent',categoryAxis:{axisLine:{show:false},axisTick:{show:false},axisLabel:{color:'#64748b',fontSize:12},splitLine:{show:false}},valueAxis:{axisLine:{show:false},axisTick:{show:false},axisLabel:{color:'#64748b',fontSize:12},splitLine:{lineStyle:{color:'#f1f5f9'}}},line:{smooth:true,symbol:'none',lineStyle:{width:2}},bar:{itemStyle:{borderRadius:[6,6,0,0]}},pie:{itemStyle:{borderRadius:6}},grid:{left:40,right:20,top:20,bottom:40,containLabel:true},tooltip:{backgroundColor:'rgba(15,23,42,0.95)',borderColor:'rgba(51,65,85,0.5)',borderWidth:1,borderRadius:12,padding:[12,16],textStyle:{color:'#fff',fontSize:13},trigger:'axis'}});
window.EChart=function(props){var ref=React.useRef(null);var chartRef=React.useRef(null);var h=props.height||400;React.useEffect(function(){if(!ref.current)return;var chart=echarts.init(ref.current,'bow');chartRef.current=chart;if(props.option)chart.setOption(props.option);var ro=new ResizeObserver(function(){chart.resize();});ro.observe(ref.current);return function(){ro.disconnect();chart.dispose();};},[]);React.useEffect(function(){if(chartRef.current&&props.option){chartRef.current.setOption(props.option,true);}},[props.option]);return React.createElement('div',{ref:ref,style:{width:'100%',height:h},className:props.className||''});};
window.LoadingSpinner=function(props){var s=props&&props.size?props.size:24;return React.createElement('svg',{xmlns:'http://www.w3.org/2000/svg',width:s,height:s,viewBox:'0 0 24 24',className:props&&props.className?props.className:''},[React.createElement('path',{key:'t',fill:'currentColor',d:'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z',opacity:'.5'}),React.createElement('path',{key:'s',fill:'currentColor',d:'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z'},React.createElement('animateTransform',{attributeName:'transform',dur:'1s',from:'0 12 12',repeatCount:'indefinite',to:'360 12 12',type:'rotate'}))]);};
window.fmt=function(n,opts){if(n==null)return'\\u2014';if(typeof n!=='number')return String(n);opts=opts||{};if(opts.currency)return new Intl.NumberFormat('en-US',{style:'currency',currency:opts.currency===true?'USD':opts.currency,maximumFractionDigits:opts.decimals!=null?opts.decimals:0}).format(n);if(opts.pct)return n.toFixed(1)+'%';if(Math.abs(n)>=1e9)return(n/1e9).toFixed(1)+'B';if(Math.abs(n)>=1e6)return(n/1e6).toFixed(1)+'M';if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+'K';return n.toLocaleString(undefined,{maximumFractionDigits:2});};
window.KPICard=function(props){var h=React.createElement;var c=props.color||'#3B82F6';var t=props.className||'bg-white border-slate-200 text-slate-900';var tc=props.titleClassName||'text-slate-500';var sc=props.subtitleClassName||'text-slate-500';return h('div',{className:'relative rounded-2xl border p-5 shadow-sm overflow-hidden '+t},[h('div',{key:'b',className:'absolute inset-x-0 top-0 h-1',style:{background:'linear-gradient(90deg, '+c+', '+c+'99)'}}),h('p',{key:'t',className:'text-xs font-medium uppercase tracking-wider mb-1 '+tc},props.title),h('p',{key:'v',className:'text-2xl font-semibold'},props.value),props.subtitle?h('p',{key:'s',className:'text-sm mt-1 '+sc},props.subtitle):null]);};
window.FilterSelect=function(props){var h=React.createElement;var label=props.label||'';var rawOpts=props.options||[];var opts=rawOpts.map(function(o){return typeof o==='object'&&o!==null?{val:o.value,lbl:o.label||String(o.value)}:{val:o,lbl:String(o)};});var selected=props.selected||[];var onChange=props.onChange||function(){};var theme=props.className||'bg-white border-slate-200 text-slate-900';var searchable=props.searchable!==undefined?props.searchable:opts.length>=8;var _s=React.useState(false),open=_s[0],setOpen=_s[1];var _q=React.useState(''),query=_q[0],setQuery=_q[1];var ref=React.useRef(null);var searchRef=React.useRef(null);React.useEffect(function(){function handleClick(e){if(ref.current&&!ref.current.contains(e.target))setOpen(false);}document.addEventListener('mousedown',handleClick);return function(){document.removeEventListener('mousedown',handleClick);};},[]);React.useEffect(function(){if(open&&searchable&&searchRef.current)searchRef.current.focus();if(!open)setQuery('');},[open]);function toggle(val){var idx=selected.indexOf(val);onChange(idx>=0?selected.filter(function(v){return v!==val;}):selected.concat([val]));}var filtered=searchable&&query?opts.filter(function(o){return o.lbl.toLowerCase().indexOf(query.toLowerCase())!==-1;}):opts;var selLabels=opts.filter(function(o){return selected.indexOf(o.val)>=0;}).map(function(o){return o.lbl;});var display=selected.length===0?'All':selLabels.length<=2?selLabels.join(', '):selected.length+' selected';return h('div',{ref:ref,className:'relative inline-block min-w-[140px]'},[label?h('label',{key:'l',className:'block text-xs font-medium opacity-60 mb-1'},label):null,h('button',{key:'btn',type:'button',className:'w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-1.5 text-sm cursor-pointer '+theme,onClick:function(){setOpen(!open);}},[h('span',{key:'t',className:'truncate'},display),h('svg',{key:'i',width:12,height:12,viewBox:'0 0 12 12',className:'opacity-50 shrink-0'},h('path',{d:'M3 5l3 3 3-3',stroke:'currentColor',strokeWidth:1.5,fill:'none'}))]),open?h('div',{key:'dd',className:'absolute z-50 mt-1 left-0 right-0 rounded-lg border shadow-lg max-h-72 overflow-auto py-1 '+theme,style:{backgroundColor:'#fff'}},[searchable?h('div',{key:'search',className:'px-2 pt-1 pb-1 sticky top-0',style:{backgroundColor:'#fff'}},[h('input',{ref:searchRef,type:'text',value:query,placeholder:'Search...',onChange:function(e){setQuery(e.target.value);},className:'w-full rounded border px-2 py-1 text-sm outline-none focus:border-blue-400 '+theme,onClick:function(e){e.stopPropagation();}})]):null,selected.length>0?h('button',{key:'clr',type:'button',className:'w-full text-left px-3 py-1.5 text-xs font-medium opacity-50 hover:opacity-100',onClick:function(){onChange([]);}},'Clear all'):null].concat(filtered.map(function(o){var isSelected=selected.indexOf(o.val)>=0;return h('label',{key:o.val,className:'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5'},[h('input',{key:'cb',type:'checkbox',checked:isSelected,onChange:function(){toggle(o.val);},className:'rounded border-slate-300 accent-blue-500'}),h('span',{key:'v',className:'truncate'},o.lbl)]);}))):null]);};
window.FilterSearch=function(props){var h=React.createElement;var label=props.label||'';var value=props.value||'';var onChange=props.onChange||function(){};var placeholder=props.placeholder||'Search...';var theme=props.className||'bg-white border-slate-200 text-slate-900';return h('div',{className:'inline-block min-w-[140px]'},[label?h('label',{key:'l',className:'block text-xs font-medium opacity-60 mb-1'},label):null,h('input',{key:'inp',type:'text',value:value,placeholder:placeholder,onChange:onChange,className:'w-full rounded-lg border px-3 py-1.5 text-sm '+theme})]);};
window.FilterDateRange=function(props){var h=React.createElement;var label=props.label||'';var value=props.value||{};var onChange=props.onChange||function(){};var theme=props.className||'bg-white border-slate-200 text-slate-900';var inputType=props.type||'date';return h('div',{className:'inline-block min-w-[200px]'},[label?h('label',{key:'l',className:'block text-xs font-medium opacity-60 mb-1'},label):null,h('div',{key:'row',className:'flex items-center gap-2'},[h('input',{key:'from',type:inputType,value:value.from||'',onChange:function(e){onChange({from:e.target.value||null,to:value.to||null});},className:'w-full rounded-lg border px-2 py-1.5 text-sm '+theme}),h('span',{key:'sep',className:'text-xs opacity-50'},'\u2013'),h('input',{key:'to',type:inputType,value:value.to||'',onChange:function(e){onChange({from:value.from||null,to:e.target.value||null});},className:'w-full rounded-lg border px-2 py-1.5 text-sm '+theme})])]);};
window.SectionCard=function(props){var h=React.createElement;var t=props.className||'bg-white border-slate-200';var tc=props.titleClassName||'text-slate-800';var sc=props.subtitleClassName||'text-slate-500';return h('div',{className:'rounded-2xl border shadow-sm p-6 '+t},[props.title?h('div',{key:'hdr',className:'mb-4'},[h('h2',{key:'t',className:'text-lg font-semibold '+tc},props.title),props.subtitle?h('p',{key:'s',className:'text-sm mt-1 '+sc},props.subtitle):null]):null,h('div',{key:'body'},props.children)]);};
""".strip()


def _find_libs_dir() -> Path | None:
    """Find the directory containing vendored JS libraries."""
    for d in _CANDIDATE_DIRS:
        if d.is_dir() and any(d.iterdir()):
            return d
    return None


@lru_cache(maxsize=1)
def _read_lib(libs_dir: Path, filename: str) -> str:
    """Read a vendored JS file and return its contents."""
    path = libs_dir / filename
    return path.read_text(encoding="utf-8")


def get_inline_scripts(mode: str = "page") -> str:
    """Return inline <script> tags with vendored JS library contents.

    Args:
        mode: 'page' for React/Babel/ECharts dashboard, 'slides' for Tailwind-only.

    Returns:
        HTML string with <script>...</script> tags containing the library code.

    Raises:
        FileNotFoundError: If vendored libs directory or individual files are missing.
            In airgapped deployments there is no CDN to fall back to, so missing
            vendored files must fail loudly.
    """
    libs_dir = _find_libs_dir()

    if libs_dir is None:
        raise FileNotFoundError(
            "Vendored JS libs directory not found. "
            "Run scripts/download-vendor-libs.sh during Docker build."
        )

    lib_files = _PAGE_LIBS if mode == "page" else _SLIDES_LIBS
    parts = []

    for filename in lib_files:
        content = _read_lib(libs_dir, filename)  # raises FileNotFoundError if missing
        parts.append(f"<script>{content}</script>")

    # Add global setup for page mode (hooks, EChart wrapper, etc.)
    if mode == "page":
        parts.append(f"<script>{_PAGE_GLOBALS}</script>")

    return "\n".join(parts)
