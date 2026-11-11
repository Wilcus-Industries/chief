"""Transcript styles: messages, markdown, activity groups (tool calls +
thinking), approval cards, pending sends. Split out of styles.py to keep it
under the length cap; concatenated into /app.css."""

CHAT_STYLES = """
.msg{max-width:78ch; margin:0 0 1.1rem; white-space:pre-wrap; word-break:break-word;}
.msg .who{display:block; font-size:11px; letter-spacing:.1em;
  text-transform:uppercase; margin-bottom:.2rem;}
.msg.owner .who{color:var(--orange);}
.msg.chief .who{color:var(--aqua);}
.msg.peer .who{color:var(--yellow);}
.msg.owner .who::before{content:"owner\\2009>\\2009";}
.msg.chief .who::before{content:"chief\\2009>\\2009";}
.msg.peer .who::after{content:"\\2009>\\2009";}
.msg.owner{padding-left:.7rem; border-left:2px solid var(--bg3);}
/* not in the stored transcript (a command and its answer): kept client-side */
.msg.eph{opacity:.8;}
.msg.eph .who::after{content:"  \\00b7  not saved"; color:var(--faint);
  text-transform:none; letter-spacing:0;}
/* chief replies rendered as markdown (script_markdown.py) */
.md{white-space:normal;}
.md > :first-child{margin-top:0;} .md > :last-child{margin-bottom:0;}
.md p, .md ul, .md ol, .md pre, .md blockquote, .md table{margin:.45rem 0;}
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6{margin:.8rem 0 .35rem;
  font-size:1em; color:var(--yellow);}
.md h1, .md h2{color:var(--orange);}
.md ul, .md ol{padding-left:3.5ch;}
.md code{background:var(--bg1); color:var(--aqua); padding:0 .25rem; border-radius:2px;}
.md pre{background:var(--bg1); border:1px solid var(--bg2); border-radius:2px;
  padding:.5rem .7rem; overflow-x:auto; white-space:pre;}
.md pre code{background:none; padding:0; color:var(--fg);}
.md blockquote{border-left:2px solid var(--bg3); padding-left:.8rem; color:var(--dim);}
.md hr{border:none; border-top:1px solid var(--bg2);}
.md a{color:var(--blue);}
.md table{display:block; overflow-x:auto; border-collapse:collapse;}
.md th, .md td{border:1px solid var(--bg2); padding:.15rem .6rem; text-align:left;}
.md th{color:var(--yellow);}
.md del{color:var(--faint);}
/* activity group: a run of tool calls + thinking, collapsed to one row */
.msg.activity{font-size:12px; color:var(--dim); white-space:normal;}
.activity summary{cursor:pointer; list-style:none; display:inline-flex;
  align-items:center; gap:.45rem; padding:.1rem .45rem .1rem .2rem;
  border-radius:2px; user-select:none;}
.activity summary::-webkit-details-marker{display:none;}
.activity summary::before{content:"\\25B8"; color:var(--faint);
  display:inline-block; transition:transform .12s;}
.activity details[open] > summary::before,
.msg.activity[open] > summary::before{transform:rotate(90deg);}
.activity summary:hover{background:var(--bg1); color:var(--fg);}
.msg.activity > summary{color:var(--blue);}
.act-list{margin:.25rem 0 0 .55rem; padding-left:.8rem;
  border-left:1px solid var(--bg2); display:flex; flex-direction:column; gap:.1rem;}
.act.tool > summary{color:var(--blue);}
.act.thinking > summary{color:var(--purple); font-style:italic;}
.act.noresult > summary{cursor:default;}
.act.noresult > summary::before{visibility:hidden;}
.msg.activity.solo > .act-list{border-left:none; margin-left:0; padding-left:.2rem;}
.msg.activity.solo > summary{display:none;}
.act-body{margin:.25rem 0 .5rem .9rem;}
.act-body pre, .think-text{margin:.2rem 0; padding:.45rem .65rem; max-height:22rem;
  overflow:auto; background:var(--bg1); border:1px solid var(--bg2);
  border-radius:2px; color:var(--fg); white-space:pre-wrap; word-break:break-word;}
.act-body .act-args{color:var(--dim);}
.think-text{color:var(--dim); font-style:italic; margin-left:.9rem;}
.act-note{color:var(--faint); font-style:italic;}
.msg.approval{border:1px solid var(--yellow); border-radius:3px;
  padding:.6rem .8rem; background:var(--bg1);}
.msg.approval .who{color:var(--yellow);}
.approval-btns{display:flex; gap:.5rem; margin-top:.5rem;}
.approval-btns button{font:inherit; font-size:12px; padding:.25rem .7rem;
  background:transparent; color:var(--aqua); border:1px solid var(--bg3);
  border-radius:2px; cursor:pointer;}
.approval-btns button:hover{background:var(--bg2);}
.approval-btns button:disabled{opacity:.5; cursor:default;}
.empty{color:var(--faint); padding:1.3rem;}
/* sent, not yet taken by a turn (script_send.py) */
#queued{padding:0 1.3rem; max-height:30vh; overflow-y:auto;}
#queued:empty{display:none;}
.msg.pending{opacity:.55; margin-bottom:.5rem;}
.ptag{margin-left:.6rem; font-size:11px; color:var(--faint);}
/* scrolled up while new output arrived */
#jump{position:absolute; right:1.6rem; bottom:4.2rem; z-index:4; font:inherit;
  font-size:12px; padding:.25rem .7rem; color:var(--bg0); background:var(--aqua);
  border:none; border-radius:2px; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.4);}
"""
