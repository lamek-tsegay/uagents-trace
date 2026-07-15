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

# Large ASCII banner for "uAgent Trace", for use as a hero element. Braille
# was tried first, but at a legible letter size each glyph only gets a
# couple of 2x4 dot-cells, which reads as noise instead of text -- a
# figlet-style block-letter banner (ANSI Shadow) is solid-filled per
# character cell instead of sub-cell dots, so it stays legible at terminal
# scale. Generated with `pyfiglet.figlet_format("uAgent Trace",
# font="ansi_shadow", width=200)`, not a runtime dependency -- the
# rendered banner is baked in as a string, same as the fetch.ai mark below.
HERO_BANNER = """\
██╗   ██╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗    ████████╗██████╗  █████╗  ██████╗███████╗
██║   ██║██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝    ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝
██║   ██║███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║          ██║   ██████╔╝███████║██║     █████╗
██║   ██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║          ██║   ██╔══██╗██╔══██║██║     ██╔══╝
╚██████╔╝██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║          ██║   ██║  ██║██║  ██║╚██████╗███████╗
 ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝          ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝
"""

# Small braille "fetch.ai" mark for use as a subordinate byline -- the
# FETCH_BRAND logo above, downsampled in the braille pixel domain (decode
# dots back to a bitmap, resize, re-pack) rather than re-rasterized from
# scratch, so it's the same traced artwork at a smaller scale.
FETCH_BRAND_SMALL = """\
⢾⡇⢸⡷⢰⣷⠀⠀⢠⡞⠓⠀⠀⠀⠀⠀⣤⠀⠀⠀⠀⠀⠀⣾⡆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠷
⣴⡆⢰⣦⢀⣄⠀⠀⢻⡟⠃⣴⠛⠛⣦⠘⣿⠛⢢⡾⠛⠻⠆⣿⡞⠛⣷⠀⠀⢠⡾⠛⠳⣿⠀⣷
⣨⡅⠈⡁⠀⡀⠀⠀⢸⡇⠘⣿⠛⠛⣛⠁⣿⠀⠸⣇⠀⢀⡀⣿⡇⠀⣿⠀⣀⢸⣇⠀⢀⣿⠀⣿
⠙⠃⠈⠁⠈⠋⠀⠀⠘⠃⠀⠈⠛⠛⠁⠀⠈⠛⠁⠙⠛⠋⠁⠙⠁⠀⠙⠈⠛⠀⠙⠛⠋⠛⠀⠋
"""
