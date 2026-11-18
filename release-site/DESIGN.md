# DataMax-LSI Release Design

## Visual world

The release station follows the supplied SYSMAX release reference: a warm
editorial canvas, near-black technical ink, one burnt-orange signal color,
compact 1240px layouts, a split product hero, restrained feature cards and a
dark download panel. Real product screenshots remain the primary material.

## Tokens

- Canvas: `#f4f1ea`
- Paper: `#fffdfa`
- Ink: `#12161b`
- Ink secondary: `#3d454e`
- Ink muted: `#737b84`
- Signal: `#c96835`
- Signal light: `#e7a06b`
- Dark panel: `#0b0f14`
- Success: `#4f8d64`
- Border on paper: `#ded9cf`
- Border on dark: `rgba(255,255,255,.10)`
- Radius: 9px controls, 18px surfaces
- UI face: self-hosted DM Sans
- Data face: self-hosted JetBrains Mono

## Composition

The landing page opens with a two-column editorial hero: copy and measurable
release facts on the left, a browser-framed real product screenshot on the
right. A compact capability marquee leads into a 3x2 feature grid and a
screenshot gallery. Download and installation are grouped in one dark panel;
release history closes the page as a compact table.

## Motion

One restrained browser settle on first load, plus 180–220 ms hover and state
transitions. Content is visible without JavaScript, and reduced-motion users
receive no entrance movement.
