# Fetch.ai brand panel — braille render of the real logo (traced from PNG).
# Monochrome: recolor to theme accent (green). Braille glyphs are widely
# supported, but confirm they render cleanly in the demo terminal font.
FETCH_BRAND = """\
⢰⣶⣶⠀⠀⣶⣶⡆⠀⣴⣶⣦⠀⠀⠀⠀⠀⢀⣴⣶⣶⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⣶⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣶⣆
⠸⠿⠿⠀⠀⠿⠿⠇⠀⠿⠿⠿⠀⠀⠀⠀⠀⣾⣿⠁⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠛⠃
⢀⣀⣀⠀⠀⣀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠰⣶⣿⣿⣶⣶⠀⢀⣴⣾⣿⣿⣷⣦⡀⠀⣶⣿⣿⣷⣶⡆⠀⣠⣶⣿⣿⣿⣶⣄⠀⢸⣿⣥⣾⣿⣿⣶⡄⠀⠀⠀⠀⠀⠀⣠⣶⣿⣿⣷⣦⣶⣶⠀⠀⣶⡆
⢸⣿⣿⠀⠀⣿⣿⡇⠀⢰⣿⡆⠀⠀⠀⠀⠀⣿⣿⠀⠀⢀⣾⡟⠁⠀⠀⠈⢻⣷⠀⠀⢸⣿⠀⠀⠀⣸⣿⠋⠀⠀⠀⠙⠟⠀⢸⣿⡏⠀⠀⠀⢻⣿⠀⠀⠀⠀⠀⣼⣿⠋⠀⠀⠀⠹⣿⣿⠀⠀⣿⡇
⠈⠉⠉⠀⠀⠉⠉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⠇⠀⢸⣿⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⢸⣿⡇⠀⠀⠀⢸⣿⠀⠀⠀⠀⠀⣿⡇⠀⠀⠀⠀⠀⣿⣿⠀⠀⣿⡇
⢠⣶⣶⠀⠀⢀⣤⠀⠀⢀⣤⡀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠈⢿⣧⡀⠀⠀⢀⣠⣄⠀⠀⢸⣿⡀⠀⠀⠹⣿⣄⠀⠀⠀⣠⣦⠀⢸⣿⡇⠀⠀⠀⢸⣿⠀⠀⣀⣀⠀⢻⣿⣄⠀⠀⠀⣰⣿⣿⠀⠀⣿⡇
⠸⠿⠿⠀⠀⠙⠟⠁⠀⠘⠿⠃⠀⠀⠀⠀⠀⠿⠿⠀⠀⠀⠈⠛⠿⣿⣿⡿⠟⠁⠀⠀⠈⠻⢿⡿⠇⠀⠙⠿⢿⣿⡿⠟⠋⠀⠸⠿⠇⠀⠀⠀⠸⠿⠀⠘⢿⡿⠀⠀⠙⠿⣿⣿⡿⠟⠿⠿⠀⠀⠿⠇

                             uAgents Trace
"""
BRAND_PANEL_WIDTH = 76

# All-caps wordmark for "uAgents Trace" (renders "TRACE" / "UAGENTS"), for
# use as the left slot of the side-by-side hero/fetch.ai lockup. Braille was
# tried first, then several mixed-case figlet fonts (`smslant`, `standard`,
# `big`) chasing a lowercase "u" -- but each of those read too thin/hollow
# next to the fetch.ai mark's solid braille fill. ANSI Shadow's solid,
# double-line block glyphs are the closest match in visual weight to that
# mark, and its being all-caps (no lowercase forms exist in this font) is an
# accepted tradeoff for that. Rendering both words on one line
# (`pyfiglet.figlet_format("uAgents Trace", ...)`) made the hero 106
# columns wide -- far wider than the mark's 72, forcing a lopsided lockup
# and a very wide side-by-side breakpoint. Rendering each word as its own
# banner (`pyfiglet.figlet_format("uAgents", font="ansi_shadow")` and
# `("Trace", font="ansi_shadow")`) and stacking them keeps the same font and
# glyphs but roughly halves the width (61 cols) at the cost of doubling the
# row count (12), which reads much closer in proportion to the mark.
#
# "Trace" (the narrower word, 41 cols) sits on top, "uAgents" (61 cols)
# below -- and "Trace" is horizontally centered over "uAgents" by a single,
# constant left-pad applied to every one of its rows (10 spaces = (61-41)//2),
# not by centering each row independently. Figlet already left-aligns every
# row of one word consistently (its rows differ only in how far right their
# content happens to reach, not in where it starts), so centering each row
# to its own individual width instead would have added a different amount
# of left-padding to whichever rows are naturally a column or two shorter
# than the word's widest row -- visibly kinking "Trace" instead of reading
# as one rigid, evenly-centered block.
#
# Not a runtime dependency -- the rendered banner is baked in as a string,
# same as the fetch.ai mark below. (Each word's trailing all-blank filler
# row -- a baseline row these letterforms don't use -- is dropped before
# stacking, same as the original single-line ANSI Shadow banner.)
HERO_BANNER = """\
          ████████╗██████╗  █████╗  ██████╗███████╗
          ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝
             ██║   ██████╔╝███████║██║     █████╗
             ██║   ██╔══██╗██╔══██║██║     ██╔══╝
             ██║   ██║  ██║██║  ██║╚██████╗███████╗
             ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝
██╗   ██╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗
██║   ██║██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝
██║   ██║███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗
██║   ██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║
╚██████╔╝██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║
 ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝
"""
