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
window.ECHARTS_TOOLTIP={backgroundColor:'rgba(15,23,42,0.95)',borderColor:'rgba(51,65,85,0.5)',borderWidth:1,borderRadius:12,padding:[12,16],textStyle:{color:'#fff',fontSize:13}};
window.EChart=function(props){var ref=React.useRef(null);var chartRef=React.useRef(null);var h=props.height||400;React.useEffect(function(){if(!ref.current)return;var chart=echarts.init(ref.current);chartRef.current=chart;if(props.option)chart.setOption(props.option);var ro=new ResizeObserver(function(){chart.resize();});ro.observe(ref.current);return function(){ro.disconnect();chart.dispose();};},[]);React.useEffect(function(){if(chartRef.current&&props.option){chartRef.current.setOption(props.option,true);}},[props.option]);return React.createElement('div',{ref:ref,style:{width:'100%',height:h},className:props.className||''});};
window.LoadingSpinner=function(props){var s=props&&props.size?props.size:24;return React.createElement('svg',{xmlns:'http://www.w3.org/2000/svg',width:s,height:s,viewBox:'0 0 24 24',className:props&&props.className?props.className:''},[React.createElement('path',{key:'t',fill:'currentColor',d:'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z',opacity:'.5'}),React.createElement('path',{key:'s',fill:'currentColor',d:'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z'},React.createElement('animateTransform',{attributeName:'transform',dur:'1s',from:'0 12 12',repeatCount:'indefinite',to:'360 12 12',type:'rotate'}))]);};
window.fmt=function(n,opts){if(n==null)return'\\u2014';if(typeof n!=='number')return String(n);opts=opts||{};if(opts.currency)return new Intl.NumberFormat('en-US',{style:'currency',currency:opts.currency===true?'USD':opts.currency,maximumFractionDigits:opts.decimals!=null?opts.decimals:0}).format(n);if(opts.pct)return n.toFixed(1)+'%';if(Math.abs(n)>=1e9)return(n/1e9).toFixed(1)+'B';if(Math.abs(n)>=1e6)return(n/1e6).toFixed(1)+'M';if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+'K';return n.toLocaleString(undefined,{maximumFractionDigits:2});};
window.KPICard=function(props){var h=React.createElement;var c=props.color||'#3B82F6';var t=props.className||'bg-white border-slate-200 text-slate-900';var tc=props.titleClassName||'text-slate-500';var sc=props.subtitleClassName||'text-slate-500';return h('div',{className:'relative rounded-2xl border p-5 shadow-sm overflow-hidden '+t},[h('div',{key:'b',className:'absolute inset-x-0 top-0 h-1',style:{background:'linear-gradient(90deg, '+c+', '+c+'99)'}}),h('p',{key:'t',className:'text-xs font-medium uppercase tracking-wider mb-1 '+tc},props.title),h('p',{key:'v',className:'text-2xl font-semibold'},props.value),props.subtitle?h('p',{key:'s',className:'text-sm mt-1 '+sc},props.subtitle):null]);};
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
