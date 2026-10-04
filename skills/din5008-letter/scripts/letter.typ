// SPDX-License-Identifier: Apache-2.0
// The copyright holder is named in the LICENSE file of this skill.
// A letter on DIN 5008:2020-03 Form B (tiefgestelltes Anschriftfeld), rendered from JSON data.
//
// Called by letter.py, which checks the data, writes it to data.json beside a copy of this file in
// a private temporary folder, and compiles there. Every user string is inserted as a string, so it
// renders as literal text and is never evaluated as Typst markup. Positions are mm from the
// top-left corner of the sheet. `<din-anchor>` metadata records where each part landed and how many
// lines it has, so letter.py can prove the layout. Needs Typst 0.15.1 or later.

#let d = json("data.json")
#let font = sys.inputs.at("font", default: "Arial")

// The DIN grid: line n starts at (n - 1) x 4,23 mm, and 4,23 mm = 12 pt (19.3). Text of any size
// sits in a 12 pt line box (top edge 9 pt above the baseline, bottom edge 3 pt below), so a blank
// line is exactly one grid line.
#let pitch = 12pt
#let grid-line(n) = (n - 1) * pitch
#let margin = (left: 25mm, right: 20mm, top: 20mm, bottom: 25mm)
#let text-width = 210mm - margin.left - margin.right

#let field(key) = {
  let v = d.at(key, default: none)
  if v == "" or v == () or v == false { none } else { v }
}
#let refuse(msg) = panic("DIN5008: " + msg)
#let lines-of(s) = s.split("\n").map(l => l.trim()).filter(l => l != "")
#let lines-in(body, width) = calc.round(measure(block(width: width, body)).height / pitch)

// A marker at the top-left of a part, with the number of grid lines the part takes.
#let anchor(name, body: none, width: text-width) = context [#metadata((
  name: name,
  x: here().position().x.mm(),
  y: here().position().y.mm(),
  page: here().page(),
  lines: if body == none { 1 } else { lines-in(body, width) },
)) <din-anchor>]

// Page background and foreground are laid out relative to the sheet, not the margins.
#let at(x, y, body) = place(top + left, dx: x, dy: y, body)
// One line of the address field, refused when wider than `width`. letter.py has already refused
// every character that could break it into two lines.
#let one-line(content, width, what) = context {
  let w = measure(content).width
  if w > width {
    refuse(what + " is " + str(calc.round(w.mm(), digits: 1)) + " mm wide, the limit is "
      + str(width.mm()) + " mm")
  }
}

#set document(title: "Brief", date: none)
// fallback: false keeps every glyph in the chosen sans font (20.7.1); letter.py refuses a PDF with an
// empty-box glyph, so a character the font lacks is refused rather than set in some other font.
// overhang: false keeps a closing full stop inside the line, so right-aligned text ends at 190 mm.
#set text(font: font, fallback: false, size: 11pt, lang: "de", region: "DE", hyphenate: true,
  overhang: false, top-edge: 9pt, bottom-edge: -3pt)
#set par(leading: 0pt, spacing: pitch, justify: false)
// Each part of the body is a block, and blocks sit one blank line apart. An anchor at the start of
// a block reports the line top; inside a paragraph it would report the baseline, 9 pt lower.
#set block(spacing: pitch)

// ---- Falzmarken 105 / 210 mm, Lochmarke 148,5 mm (Bild 9), on every page ----
// Length and stroke are not normed. The marks start 5 mm in, because most printers cannot print
// the outer 4 mm of the sheet.
#let mark(y, len, name) = at(5mm, y, [#anchor(name)#line(length: len, stroke: 0.5pt + black)])
#let marks = { mark(105mm, 5mm, "fold-1"); mark(148.5mm, 8mm, "hole"); mark(210mm, 5mm, "fold-2") }

// ---- Briefkopf, 0 to 45 mm: the sender, right-aligned to the 190 mm text edge (E.8, E.9) ----
// It ends at 40 mm, 5 mm above the Anschriftfeld, and may start no higher than 5 mm.
#let sender = lines-of(d.sender)
#let head = block(width: text-width, align(right, {
  anchor("sender")
  text(size: 14pt, weight: "bold", top-edge: 12pt, bottom-edge: -4pt, sender.first())
  for l in sender.slice(1) { linebreak(); text(size: 9pt, l) }
}))
#let head-check = context {
  for (i, l) in sender.enumerate() {
    let w = measure(text(size: if i == 0 { 14pt } else { 9pt }, weight: if i == 0 { "bold" } else { "regular" }, l)).width
    if w > text-width { refuse("sender line " + str(i + 1) + " is wider than the 165 mm text width") }
  }
  if measure(head).height > 35mm {
    refuse("the sender does not fit the 35 mm left for it in the 45 mm header (" + str(sender.len()) + " lines)")
  }
}

// ---- Anschriftfeld, 85 x 45 mm at x 20, y 45 to 90 mm; writing from x 25, max 80 mm ----
// Zusatz- und Vermerkzone 45 to 62,7 mm. On the 12 pt grid of Tabelle B.1 it holds grid lines 13,
// 14 and 15 (50,8 / 55,0 / 59,2 mm). Line 13 takes the Rücksendeangabe; Zusätze und Vermerke fill
// lines 14 and 15 from the bottom (20.7.1). Anschriftzone 62,7 to 90 mm: six lines from grid line 16
// (63,5 mm, B.1).
#let ret = field("return_line")
#let ret-auto = d.at("return_line_auto", default: false)
#let notes = field("notes")
#let recipient = lines-of(d.recipient)
// A Rücksendeangabe the caller gave is refused when it does not fit at 8 pt. The one this tool
// derives from the sender may drop to 6 pt, the smallest size 19.4.3 allows, and is left out when
// even that does not fit.
#let address-field = {
  if ret != none {
    context {
      let sizes = if ret-auto { (8pt, 7pt, 6pt) } else { (8pt,) }
      let size = sizes.find(s => measure(text(size: s, ret)).width <= 80mm)
      if size == none and not ret-auto {
        one-line(text(size: 8pt, ret), 80mm, "the Rücksendeangabe")
      } else if size == none {
        at(25mm, grid-line(13), anchor("return-line-omitted"))
      } else {
        let t = text(size: size, ret)
        one-line(t, 80mm, "the Rücksendeangabe")
        at(25mm, grid-line(13), [#anchor("return-line")#t])
      }
    }
  }
  if notes != none {
    if notes.len() > 2 { refuse("at most 2 lines of Zusätze und Vermerke, not " + str(notes.len())) }
    for (i, n) in notes.enumerate() {
      let t = text(size: 9pt, n)
      one-line(t, 80mm, "Vermerk line " + str(i + 1))
      at(25mm, grid-line(15 - (notes.len() - 1 - i)), [#anchor("note-" + str(i + 1))#t])
    }
  }
  if recipient.len() > 6 {
    refuse("the Anschriftzone has 6 lines, the address has " + str(recipient.len()))
  }
  for (i, l) in recipient.enumerate() {
    let t = text(l)
    one-line(t, 80mm, "address line " + str(i + 1))
    at(25mm, grid-line(16 + i), [#anchor("recipient-" + str(i + 1))#t])
  }
}

// ---- Informationsblock: x 125 to 200 mm, first line on grid line 13 (19.5, A.1, B.1) ----
#let labels = (
  your_ref: "Ihr Zeichen", your_message: "Ihre Nachricht vom",
  our_ref: "Unser Zeichen", our_message: "Unsere Nachricht vom",
  name: "Name", phone: "Telefon", fax: "Fax", email: "E-Mail",
)
#let rows-for(keys) = keys.filter(k => field(k) != none).map(k => (labels.at(k), field(k)))
#let extra = if field("info") != none { field("info").map(p => (p.at(0), p.at(1))) } else { () }
// Reference signs, then contact, then Datum, each group set off by a blank line (19.5).
#let groups = (
  rows-for(("your_ref", "your_message", "our_ref", "our_message")),
  rows-for(("name", "phone", "fax", "email")) + extra,
  if field("date") != none { (("Datum", field("date")),) } else { () },
).filter(g => g.len() > 0)
// A value set at 11 pt, or smaller down to 8 pt when it is one long token such as an e-mail
// address that does not fit its column (20.8 allows that). Values are not hyphenated, so the text
// on the page is the text given. Needs context: the column width depends on the widest label.
#let info-grid() = {
  let label-w = calc.max(..groups.flatten().chunks(2).map(((k, v)) => measure(text(size: 8pt, k)).width), 0pt)
  if label-w > 40mm {
    refuse("an Informationsblock label is " + str(calc.round(label-w.mm(), digits: 1))
      + " mm wide, the limit is 40 mm so the values keep their column: shorten the label")
  }
  let value-w = 75mm - label-w - 3mm
  let value(v) = {
    let fit = (11pt, 10pt, 9pt, 8pt).find(s => measure(text(size: s, v)).width <= value-w)
    // A token too wide even at 8 pt stays at 8 pt and may wrap after "@", "." or a hyphen: an
    // invisible zero-width space after "@" and "." gives Typst a break point and prints nothing.
    let size = if v.contains(" ") { 11pt } else if fit == none { 8pt } else { fit }
    let shown = if not v.contains(" ") and fit == none { v.replace("@", "@\u{200B}").replace(".", ".\u{200B}") } else { v }
    text(size: size, hyphenate: false, shown)
  }
  grid(columns: (auto, 1fr), column-gutter: 3mm,
    ..groups.enumerate().map(((gi, g)) => {
      let cells = g.map(((k, v)) => ([#anchor("info-row")#text(size: 8pt, k)], value(v))).flatten()
      if gi > 0 { (grid.cell(colspan: 2, box(height: pitch)),) + cells } else { cells }
    }).flatten())
}
#let info-lines() = if groups.len() == 0 { 0 } else { lines-in(info-grid(), 75mm) }

#let multi(s) = lines-of(s).join(linebreak())
#let paras-of(s) = s.split(regex("\n[ \t]*\n")).map(t => t.trim()).filter(t => t != "")
// Text in blank-line separated paragraphs, one block: a blank line in the input stays one blank line.
#let paragraphs-body(s) = paras-of(s).map(multi).join(parbreak())

#set page(
  paper: "a4",
  fill: white, // an opaque preview PNG, readable on a dark page too
  margin: margin,
  background: marks,
  foreground: context if here().page() == 1 {
    head-check
    // The Gruß block is unbreakable, so it must fit a page with room to spare.
    let closing-size = (if field("closing") == none { 0 } else { lines-in(paragraphs-body(field("closing")), text-width) }) + (if field("signer") == none { 0 } else { lines-in(multi(field("signer")), text-width) + 3 })
    if closing-size > 20 { refuse("the Gruß and the Unterzeichner take " + str(closing-size) + " lines, more than the 20 that stay together on a page") }
    at(25mm, 40mm - measure(head).height, head)
    address-field
    if groups.len() > 0 {
      if 12 + info-lines() > 40 {
        refuse("the Informationsblock runs below grid line 40 and leaves no room for the letter")
      }
      let g = info-grid()
      at(125mm, grid-line(13), block(width: 75mm, [#anchor("info", body: g, width: 75mm)#g]))
    }
  },
  // Page numbers on every page once there are two, a blank line below the text (20.17, E.4).
  footer: context {
    let total = counter(page).final().first()
    if total > 1 {
      align(right, text(size: 9pt)[#anchor("page-number")Seite #counter(page).get().first() von #total])
    }
  },
)

// ---- Body. The Betreff starts two blank lines below the lower of the Anschriftzone (last line
// 21) and the Informationsblock (20.9.2), so on grid line 24 (97,4 mm) or lower. ----
#context {
  // A block, not a bare v(): an inline context would open a paragraph and add its line box.
  // The first body block adds one blank line of spacing above itself, hence the - pitch.
  let last = calc.max(21, if groups.len() == 0 { 0 } else { 12 + info-lines() })
  block(spacing: 0pt, height: grid-line(last + 3) - margin.top - pitch)
}

// One measured part of the body. `sticky` keeps it on the same page as the part after it.
#let part(name, body, sticky: false) = block(sticky: sticky)[#anchor(name, body: body)#body]
#let subject = field("subject")
#if subject != none {
  part("subject", text(weight: "bold", multi(subject)))
  v(pitch) // two blank lines before the Anrede (20.9.2)
}

#let salutation = field("salutation")
#if salutation != none {
  part("salutation", salutation)
}

#let paragraphs = if field("body") == none { () } else { paras-of(field("body")) }
#let closing = field("closing")
#let signer = field("signer")
#let enclosures = field("enclosures")
#let cc = field("cc")

// 20.10: when the Gruß lands on a later page, two lines of the text must stand above it. letter.py
// lays the letter out plainly first (keep 0). When that leaves fewer than two lines above the Gruß,
// it lays it out again with keep = 2, 3, ... : the last `keep` lines of the text stick to the Gruß
// and move with it. Those lines come from the end of the last paragraph, or from the last paragraphs
// when they are shorter. A paragraph is cut at one of the line breaks the author typed, or between
// two words of one typed line where Typst breaks it anyway: ragged-right text breaks greedily, so a
// prefix breaks where the whole line does. letter.py keeps a cut layout only when its printed lines
// are the same as those of the plain layout, so a cut that would show is never used.
#let keep = int(sys.inputs.at("keep", default: "0"))
// Cut paragraph `p` (counts: its line count) so that the last `need` lines form the tail. Returns
// (head, tail) as strings with typed line breaks, or none when no cut leaves a head of two lines.
#let cut(p, need) = {
  let hard = lines-of(p)
  let c = hard.map(h => int(lines-in(h, text-width)))
  // Whole typed lines from the end, while they are not more than is needed.
  let j = hard.len()
  let have = 0
  while j > 0 and have + c.at(j - 1) <= need { j -= 1; have += c.at(j) }
  let head = hard.slice(0, j)
  let tail = hard.slice(j)
  if have < need and j > 0 {
    // Cut typed line j - 1 between words so that its last (need - have) lines join the tail.
    let toks = hard.at(j - 1).split(" ")
    let keep-head = c.at(j - 1) - (need - have)
    let lo = 0
    let hi = toks.len() - 1
    while lo < hi {
      let mid = calc.quo(lo + hi + 1, 2)
      if int(lines-in(toks.slice(0, mid).join(" "), text-width)) <= keep-head { lo = mid } else { hi = mid - 1 }
    }
    if lo > 0 {
      head = hard.slice(0, j - 1) + (toks.slice(0, lo).join(" "),)
      tail = (toks.slice(lo).join(" "),) + hard.slice(j)
    }
  }
  if head.len() == 0 or tail.len() == 0 { return none }
  if int(lines-in(head.join("\n"), text-width)) < 2 { return none }
  (head.join("\n"), tail.join("\n"))
}
#context {
  let counts = paragraphs.map(p => int(lines-in(multi(p), text-width)))
  let closes = closing != none or signer != none
  // From the end: whole paragraphs stick while they are no longer than what is still needed; the
  // paragraph that crosses the need is cut, or sticks whole when no cut leaves it a two-line head.
  let need = if closes { keep } else { 0 }
  let plan = paragraphs.map(_ => none)
  let i = paragraphs.len() - 1
  while need > 0 and i >= 0 {
    let pieces = if counts.at(i) > need { cut(paragraphs.at(i), need) } else { none }
    plan.at(i) = if pieces == none { "sticky" } else { pieces }
    need = calc.max(0, need - counts.at(i))
    i -= 1
  }
  for (i, p) in paragraphs.enumerate() {
    let name = if i == 0 { "body" } else { "paragraph-" + str(i + 1) }
    let how = plan.at(i)
    if type(how) == array {
      let (head, tail) = how
      block(below: 0pt)[#anchor(name, body: multi(head))#multi(head)]
      block(above: 0pt, sticky: true)[#anchor("tail-of-" + name, body: multi(tail))#multi(tail)]
    } else {
      part(name, multi(p), sticky: how == "sticky")
    }
  }
}

// Gruß, then the Unterzeichner after three blank lines left for the signature (20.11, 20.13; the
// number of blank lines is this template's choice, the norm leaves it to need). A blank line inside
// the closing stays, so the company name can follow the Gruß after one blank line (20.12).
// The block is unbreakable and short by construction. It sticks to the Anlagen and the Verteiler
// that follow; their own blocks break like text, so a long list continues on the next page.
#if closing != none or signer != none {
  block(breakable: false, sticky: enclosures != none or cc != none, {
    if closing != none { part("closing", paragraphs-body(closing)) }
    if signer != none {
      // Block spacing already gives one blank line; v() adds the other two.
      if closing != none { v(2 * pitch) }
      part("signer", multi(signer))
    }
  })
}

// Anlagen at least three blank lines below the Gruß: one below the Unterzeichner, who already
// stands three below it (20.15.2). Verteiler one blank line after Anlagen (20.15.3).
#if (enclosures != none or cc != none) and closing != none and signer == none { v(2 * pitch) }
#let labelled(name, singular, plural, items, sticky: false) = if items != none {
  let body = [#text(weight: "bold", if items.len() == 1 { singular } else { plural })#linebreak()#items.join(linebreak())]
  part(name, body, sticky: sticky)
}
#labelled("enclosures", "Anlage", "Anlagen", enclosures)
#labelled("cc", "Verteiler", "Verteiler", cc)
