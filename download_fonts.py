"""
Download Google Fonts used by ReelForge templates.
Runs once during Docker build.
"""
import os
import re
import urllib.request

FONT_DIR = "/usr/local/share/fonts/google"
os.makedirs(FONT_DIR, exist_ok=True)

# (family_param, name_to_save)
# family_param format: "Family+Name:ital,wght@0,700" or "Family+Name:wght@300"
FONTS = [
    ("Oswald:wght@700",                          "Oswald-Bold"),
    ("Zilla+Slab:wght@700",                      "ZillaSlab-Bold"),
    ("Cormorant+Garamond:wght@300",              "CormorantGaramond-Light"),
    ("Cinzel:wght@700",                          "Cinzel-Bold"),
    ("Courier+Prime:wght@700",                   "CourierPrime-Bold"),
    ("Raleway:wght@100",                         "Raleway-Thin"),
    ("Bodoni+Moda:ital,wght@0,700",             "BodoniModa-Bold"),
    ("Libre+Baskerville:ital,wght@1,400",        "LibreBaskerville-Italic"),
    ("Bebas+Neue:wght@400",                      "BebasNeue-Regular"),
    ("EB+Garamond:wght@400",                     "EBGaramond-Regular"),
    ("Roboto+Slab:wght@300",                     "RobotoSlab-Light"),
    ("Crimson+Text:wght@700",                    "CrimsonText-Bold"),
    ("Lora:wght@600",                            "Lora-SemiBold"),
    ("Josefin+Sans:wght@100",                    "JosefinSans-Thin"),
    ("DM+Serif+Display:wght@400",               "DMSerifDisplay-Regular"),
    ("Abril+Fatface:wght@400",                   "AbrilFatface-Regular"),
    ("Cardo:ital,wght@1,400",                    "Cardo-Italic"),
    ("Montserrat:wght@700",                      "Montserrat-Bold"),
    ("Spectral:wght@300",                        "Spectral-Light"),
    ("Playfair+Display:ital,wght@1,700",         "PlayfairDisplay-BoldItalic"),
    # ── 8 new templates (21-28) ────────────────────────────────────────────────
    ("Great+Vibes:wght@400",                     "GreatVibes-Regular"),
    ("Dancing+Script:wght@700",                  "DancingScript-Bold"),
    ("Playfair+Display:wght@900",               "PlayfairDisplay-Black"),   # Heritage template
    ("Italiana:wght@400",                        "Italiana-Regular"),
    ("Anton:wght@400",                           "Anton-Regular"),
    ("Pacifico:wght@400",                        "Pacifico-Regular"),
    ("Unbounded:wght@700",                       "Unbounded-Bold"),
    ("Noto+Serif:ital,wght@1,400",              "NotoSerif-Italic"),
]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

for family_param, name in FONTS:
    dest = f"{FONT_DIR}/{name}.ttf"
    if os.path.exists(dest):
        print(f"  skip (exists): {name}")
        continue
    try:
        url = f"https://fonts.googleapis.com/css2?family={family_param}&display=swap"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        css = urllib.request.urlopen(req, timeout=15).read().decode()
        ttf_urls = re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", css)
        if not ttf_urls:
            print(f"  WARN no TTF URL found for {name}")
            continue
        urllib.request.urlretrieve(ttf_urls[0], dest)
        print(f"  ok: {name}")
    except Exception as e:
        print(f"  FAIL {name}: {e}")

# Rebuild font cache
os.system("fc-cache -f -v > /dev/null 2>&1")
print("Font download complete.")
