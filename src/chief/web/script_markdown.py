"""JS markdown renderer for chief's replies in the web transcript.

Text is parsed into a plain ``{tag, children}`` tree, then built with
createElement/textContent only — never innerHTML — so model output (which can
echo untrusted text: a web page, a stranger's message) cannot inject markup.
Tags come only from the parser's own literals; links keep http(s)/mailto
targets and drop the rest. Covers what replies actually use: fences, headings,
lists, quotes, rules, GFM tables, and inline code/bold/italic/strike/links.
A raw string so the JS regexes read as written; concatenated into app.js.
"""

MARKDOWN_SCRIPT = r"""
/* ---- markdown ---- */
const MD_SAFE_URL = /^(https?:|mailto:)/i;
const MD_FENCE = /^\s*(```|~~~)/;
const MD_ITEM = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
const MD_TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const MD_BLOCK_START = /^(\s*(```|~~~)|#{1,6}\s|\s*>|\s*([-*+]|\d+[.)])\s)/;
const MD_INLINE = new RegExp([
  "`([^`\\n]+)`",                              /* 1 code */
  "\\*\\*([\\s\\S]+?)\\*\\*|__([\\s\\S]+?)__", /* 2,3 strong */
  "\\*([^*\\s](?:[^*]*[^*\\s])?)\\*",          /* 4 em */
  "(?<![\\w])_([^_\\s](?:[^_]*[^_\\s])?)_(?![\\w])",  /* 5 em, not snake_case */
  "~~([\\s\\S]+?)~~",                          /* 6 del */
  "\\[([^\\]]+)\\]\\(([^)\\s]+)\\)",           /* 7,8 link */
].join("|"), "g");

function mdInline(text){
  const out = []; let last = 0;
  for (const m of text.matchAll(MD_INLINE)){
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push({tag: "code", children: [m[1]]});
    else if (m[2] != null || m[3] != null)
      out.push({tag: "strong", children: mdInline(m[2] ?? m[3])});
    else if (m[4] != null || m[5] != null)
      out.push({tag: "em", children: mdInline(m[4] ?? m[5])});
    else if (m[6] != null) out.push({tag: "del", children: mdInline(m[6])});
    else out.push(MD_SAFE_URL.test(m[8])
      ? {tag: "a", href: m[8], children: mdInline(m[7])} : m[0]);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/* single newlines inside a paragraph are kept: chat replies rely on them */
function mdLines(lines){
  const out = [];
  lines.forEach((l, i) => { if (i) out.push({tag: "br"}); out.push(...mdInline(l)); });
  return out;
}

const mdCells = (line) => line.trim().replace(/^\||\|$/g, "").split("|")
  .map((c) => mdInline(c.trim()));

function mdList(lines, i){
  const ordered = /\d/.test(lines[i].match(MD_ITEM)[1]);
  const node = {tag: ordered ? "ol" : "ul", children: []};
  if (ordered) node.start = parseInt(lines[i], 10);
  let item = null;
  while (i < lines.length){
    const m = lines[i].match(MD_ITEM);
    if (m && /\d/.test(m[1]) === ordered){
      item = [m[2]]; node.children.push(item); i++;
    } else if (item && lines[i].trim() && /^\s{2,}/.test(lines[i])){
      item.push(lines[i].trim()); i++;
    } else if (!lines[i].trim() && MD_ITEM.test(lines[i + 1] || "")){
      i++;  /* "1. a\n\n2. b" is still one list */
    } else break;
  }
  node.children = node.children.map((it) => ({tag: "li", children: mdLines(it)}));
  return [node, i];
}

function mdTable(lines, i){
  const head = mdCells(lines[i]), rows = []; i += 2;
  while (i < lines.length && lines[i].includes("|")) rows.push(mdCells(lines[i++]));
  const row = (cells, tag) => ({tag: "tr",
    children: cells.map((c) => ({tag, children: c}))});
  return [{tag: "table", children: [
    {tag: "thead", children: [row(head, "th")]},
    {tag: "tbody", children: rows.map((r) => row(r, "td"))}]}, i];
}

function mdBlocks(src){
  const lines = src.replace(/\r\n?/g, "\n").replace(/\n+$/, "").split("\n");
  const out = []; let i = 0, m, node;
  while (i < lines.length){
    const line = lines[i];
    if ((m = line.match(MD_FENCE))){
      const body = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(m[1]))
        body.push(lines[i++]);
      i++;  /* closing fence; absent mid-stream, so EOF closes it */
      out.push({tag: "pre", children: [{tag: "code", children: [body.join("\n")]}]});
    } else if ((m = line.match(/^(#{1,6})\s+(.*?)(\s+#+)?\s*$/))){
      out.push({tag: "h" + m[1].length, children: mdInline(m[2])}); i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){
      out.push({tag: "hr"}); i++;
    } else if (/^\s*>/.test(line)){
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i]))
        body.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push({tag: "blockquote", children: mdBlocks(body.join("\n"))});
    } else if (MD_ITEM.test(line)){
      [node, i] = mdList(lines, i); out.push(node);
    } else if (line.includes("|") && MD_TABLE_SEP.test(lines[i + 1] || "")){
      [node, i] = mdTable(lines, i); out.push(node);
    } else if (!line.trim()){
      i++;
    } else {
      const body = [lines[i++]];
      while (i < lines.length && lines[i].trim() && !MD_BLOCK_START.test(lines[i]))
        body.push(lines[i++]);
      out.push({tag: "p", children: mdLines(body)});
    }
  }
  return out;
}

function mdNode(n){
  if (typeof n === "string") return document.createTextNode(n);
  const e = document.createElement(n.tag);
  if (n.href){ e.href = n.href; e.target = "_blank"; e.rel = "noopener noreferrer"; }
  if (n.start > 1) e.start = n.start;
  (n.children || []).forEach((c) => e.appendChild(mdNode(c)));
  return e;
}

function setMarkdown(node, text){ node.replaceChildren(...mdBlocks(text).map(mdNode)); }
"""
