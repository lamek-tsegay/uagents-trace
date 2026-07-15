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

                              uAgent Trace
"""
BRAND_PANEL_WIDTH = 76

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
