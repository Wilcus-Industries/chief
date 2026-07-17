"""The web UI stylesheet, served at /app.css. A gruvbox terminal console."""

STYLES = """
:root{
  --bg0:#1d2021; --bg1:#282828; --bg2:#3c3836; --bg3:#504945;
  --fg:#ebdbb2; --dim:#a89984; --faint:#7c6f64;
  --orange:#fe8019; --yellow:#d79921; --aqua:#8ec07c;
  --blue:#83a598; --red:#fb4934;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{
  margin:0; height:100vh; display:flex; flex-direction:column;
  background:var(--bg0); color:var(--fg);
  font:14px/1.55 ui-monospace,"DejaVu Sans Mono","SFMono-Regular",Menlo,monospace;
}
/* status bar — the daemon's tmux-style line */
#statusbar{
  display:flex; align-items:stretch; height:26px; flex:0 0 26px;
  background:var(--bg1); border-bottom:1px solid var(--bg2);
  font-size:12px; user-select:none;
}
.seg{display:flex; align-items:center; padding:0 .8rem; color:var(--dim);
  border-right:1px solid var(--bg2); white-space:nowrap;}
.seg.grow{flex:1; border-right:none;}
.seg.brand{color:var(--bg0); background:var(--orange); font-weight:700;
  letter-spacing:.06em;}
.seg.buf{color:var(--fg);}
#mon-seg{border-right:none; border-left:1px solid var(--bg2); color:var(--yellow);}
.dot{width:7px; height:7px; border-radius:50%; background:var(--faint);
  margin-right:.5rem; box-shadow:0 0 6px transparent;}
#conn.live .dot{background:var(--aqua); box-shadow:0 0 6px var(--aqua);}
#conn.down .dot{background:var(--red);}
/* body split */
#main{flex:1; display:flex; min-height:0;}
#buffers{
  flex:0 0 220px; display:flex; flex-direction:column; min-height:0;
  background:var(--bg1); border-right:1px solid var(--bg2);
}
.panel-label{padding:.6rem .9rem .4rem; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--faint);}
#buflist{list-style:none; margin:0; padding:0; overflow-y:auto; flex:1;}
#buflist li{
  display:flex; align-items:baseline; gap:.55rem; padding:.32rem .9rem;
  cursor:pointer; color:var(--dim); border-left:2px solid transparent;
}
#buflist li:hover{background:var(--bg2); color:var(--fg);}
#buflist li.active{background:var(--bg0); color:var(--fg);
  border-left-color:var(--orange);}
#buflist .idx{color:var(--yellow); font-size:12px; min-width:1ch;}
#buflist .name{flex:1; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap;}
#buflist .badge{font-size:10px; color:var(--faint); text-transform:uppercase;
  letter-spacing:.08em;}
#buflist li.unread .name::after{content:" \\25CF"; color:var(--aqua);
  font-size:10px;}
#newbuf{
  margin:.5rem; padding:.45rem; font:inherit; text-align:left;
  background:transparent; color:var(--aqua); border:1px dashed var(--bg3);
  border-radius:2px; cursor:pointer;
}
#newbuf:hover{background:var(--bg2); border-style:solid;}
/* transcript pane */
#pane{flex:1; display:flex; flex-direction:column; min-height:0; position:relative;}
#log{flex:1; overflow-y:auto; padding:1.1rem 1.3rem;}
.msg{max-width:74ch; margin:0 0 1rem; white-space:pre-wrap; word-break:break-word;}
.msg .who{display:block; font-size:11px; letter-spacing:.1em;
  text-transform:uppercase; margin-bottom:.15rem;}
.msg.owner .who{color:var(--orange);}
.msg.chief .who{color:var(--aqua);}
.msg.owner .who::before{content:"owner\\2009>\\2009";}
.msg.chief .who::before{content:"chief\\2009>\\2009";}
.empty{color:var(--faint); padding:1.3rem;}
#readonly{padding:.5rem 1.3rem; font-size:12px; color:var(--yellow);
  background:var(--bg1); border-top:1px solid var(--bg2);}
/* prompt line */
#f{display:flex; align-items:center; gap:.6rem; padding:.7rem 1.3rem;
  border-top:1px solid var(--bg2); background:var(--bg1); position:relative;}
#f.disabled{opacity:.5; pointer-events:none;}
.sigil{color:var(--orange); flex:0 0 auto; font-weight:700;}
#input{flex:1; font:inherit; color:var(--fg); background:transparent;
  border:none; outline:none; caret-color:var(--orange);}
#input::placeholder{color:var(--faint);}
/* readline-style completion popup */
#complete{
  position:absolute; left:1.3rem; bottom:calc(100% - .2rem); margin:0;
  padding:.25rem 0; list-style:none; min-width:16rem;
  background:var(--bg2); border:1px solid var(--bg3); border-radius:2px;
  box-shadow:0 -6px 18px rgba(0,0,0,.4); z-index:5;
}
#complete li{padding:.25rem .8rem; color:var(--dim); cursor:pointer;}
#complete li .hit{color:var(--orange);}
#complete li.sel{background:var(--bg3); color:var(--fg);}
/* focus + motion */
:focus-visible{outline:2px solid var(--blue); outline-offset:1px;}
@media (max-width:640px){#buffers{flex-basis:150px}}
"""
