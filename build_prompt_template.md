# Build prompt template

Second LLM call after synthesis. Input: the internal brief (Part A of
brief_template.md). Output: one prompt Yasmin can paste into a
coding agent (Claude Code or Codex) to start building the site.

Instructions are written in English (builders follow them more
reliably); all client content stays in Hebrew, verbatim from the brief.

Fixed blocks: every section below titled `## {NAME} — …` is inserted
by code, verbatim, wherever the output has the line `{NAME}`. The LLM
never writes or rewrites them.

---

## Writing rules (for the LLM)

- Use only what is in the brief. Do not invent services, texts, claims,
  testimonials, prices or contact details. Say this once, in the
  "Ground rules" block — do not repeat it in every section.
- Anything missing → an explicit placeholder in brackets ending with
  "— להשלמה", e.g. `[טקסט אודות — להשלמה]`, `[תמונת צוות — להשלמה]`.
  Gender-neutral; never fill gaps with generic copy.
- Client quotes and Hebrew content → copied verbatim.
- Suggested sitemap and sections are allowed — a starting point derived
  from goals, features and audience. Mark them "suggested".
- Build vs. defer: a feature whose scope is not settled in the brief
  (who uses it, what data, how it connects) goes under "Deferred" — it
  is NOT built or scaffolded, not even as empty screens. Only build
  what is decided.
- Stack: Astro when everything in scope is a public site (pages,
  content, contact links, a simple form). Next.js only when a feature
  that is in scope (not deferred) needs app logic: login / personal
  area, shop or payments, booking stored in a database, dashboards.
  Write the choice and the one-line reason.
- Style "עוד לא יודעים" (or no style given) → the design section says
  so, and the work plan starts with 2–3 homepage directions for Yasmin
  to choose from. Do not write generic "suggested style" guidance.
- Ignore the brief's "שימי לב" and internal notes, except where they
  change what gets built or deferred.
- Keep it scannable: headings and bullets, no long paragraphs.

---

## Output structure

```
# Build: {business} website

## Ground rules
{GROUND_RULES}

## Project
One paragraph: what the business does, who the site is for, and the
one main action the site must drive (call / purchase / booking / lead).

## Stack
- Astro or Next.js — one-line reason (see writing rules)

## Audience
- Typical client, what they look for, what decides their choice
- Common questions the site should answer

## Sitemap (suggested)
- Page — purpose — key sections, in order
  (derived from goals + features; e.g. booking → booking page/flow,
  portfolio → projects page, FAQ goal → FAQ section)

## Functionality
- Each feature in scope with what the brief says about it (system
  names, integrations, number of products, languages)
- Forms: fields and where submissions go (if stated)

## Design direction
- Style: {style} — translate into concrete guidance (spacing,
  typography feel, imagery). If unknown: say so; the work plan covers it
- Inspiration: each URL + what to take from it (and what to avoid)
- Colors: brand colors (hex if given) / direction / colors to avoid
- Logo: provided / coming {date} → use text placeholder / none →
  business name as a simple text wordmark (never a designed logo)

## Content
- Texts: who writes them; until then, placeholders per section. If
  the brief says the client wants a basic draft to edit, write here:
  "Draft texts approved"
- Images: what exists / planned / stock — placeholders labeled by type
- Client's own words worth using in copy (verbatim quotes)

## Deferred (do not build)
- Each feature whose scope is not settled + what must be decided first.
  Omit this section if nothing is deferred.

## How to work
{HOW_TO_WORK}

## Requirements
{STANDARDS}

## Open items
- Anything missing that blocks a section — built with placeholders
  meanwhile — and anything to confirm before launch
```

---

## {GROUND_RULES} — fixed, inserted as-is

- Everything client-specific in this prompt comes from a client
  interview. Do not invent services, copy, claims, testimonials,
  prices, contact details, links or reference sites.
- Exception — only if the Content section says "Draft texts
  approved": write short, plain Hebrew draft copy per section, using
  only facts stated in this prompt (no claims, numbers, testimonials
  or credentials that aren't here). Mark each draft visibly
  `[טיוטה — לעריכה]`; the client edits it. This is not copywriting.
- Missing content → keep the bracketed placeholder (`[… — להשלמה]`)
  visible in the UI, so it is obvious what still needs to arrive.
- Hebrew text in this prompt is final client wording — use it
  verbatim, do not "improve" it.
- When something is unclear, ask — do not guess.
- No branding work: do not design a logo, icon or brand identity. If
  there is no logo, use the business name as a simple text wordmark.

## {HOW_TO_WORK} — fixed, inserted as-is

1. Read this whole prompt. Reply with a short plan (pages, components,
   stack setup) and any questions. Wait for approval before coding.
2. If the design style is not set: build 2–3 distinct homepage hero
   directions (same content, different look). Stop and wait for
   Yasmin to pick one.
3. Set up the project and design tokens (colors, type scale, spacing)
   from the chosen direction.
4. Build the layout (header, footer, RTL, mobile nav), then the
   homepage. Stop for review.
5. Build the remaining pages. Stop for review.
6. Before calling it done: check every page at 375px and desktop, RTL
   correctness, keyboard navigation, contrast, alt text, and a
   Lighthouse run (aim for 90+ in every category). Report results and
   the list of placeholders still open.
- Client materials go in `/content` (texts) and `/public/images`
  (images and logo) as they arrive. Until then, use placeholders.
- Never build anything listed under "Deferred".

## {STANDARDS} — Yasmin's fixed requirements (edit freely)

- Hebrew, RTL layout throughout (and per-language direction if
  multilingual)
- Mobile-first, fully responsive
- Accessibility per Israeli standard IS 5568 (WCAG 2.0 AA): semantic
  HTML, contrast, keyboard navigation, alt text, accessibility
  statement page
- Basic SEO: page titles, meta descriptions, Open Graph, sitemap
- Fast loading: optimized images, no unnecessary libraries
- Privacy policy page; any form has a consent checkbox
- Hosting: Vercel
- Database only when an in-scope feature needs one (form submissions,
  booking, shop, personal area): Supabase or Neon
