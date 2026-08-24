"""Generate synthetic proof photos so the vision path can be tested for real.

Real photos of a real desk would be better, but they'd have to live in the repo
and get shipped to Google on every test run. These are crude but they exercise
the two things the validator actually decides: is there a readable clock, and
does the scene match the committed task.
"""
import os, sys
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_img")
os.makedirs(OUT, exist_ok=True)

def font(size):
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()

def stamp(d, w, h, when, on=True):
    if not on: return
    txt = when.strftime("%d.%m.%Y %H:%M")
    f = font(34)
    tw = d.textlength(txt, font=f)
    d.rectangle([w-tw-30, h-62, w-10, h-14], fill=(0,0,0))
    d.text((w-tw-20, h-56), txt, fill=(255,214,0), font=f)

def desk_with_maths(when, stamped=True, path="work_ok.jpg"):
    w,h = 900,650
    im = Image.new("RGB",(w,h),(38,32,28)); d = ImageDraw.Draw(im)
    d.rectangle([60,90,840,560], fill=(246,244,238))              # paper
    d.text((100,120), "Linear Algebra — Problem Set 3", fill=(20,20,20), font=font(30))
    d.text((100,175), "Eigenvalues and Eigenvectors", fill=(90,90,90), font=font(22))
    ys=230
    for line in ["det(A - λI) = 0","| 4-λ   2   |","|  1   3-λ  |  = 0",
                 "(4-λ)(3-λ) - 2 = 0","λ² - 7λ + 10 = 0","λ = 5,  λ = 2"]:
        d.text((120,ys), line, fill=(25,35,90), font=font(26)); ys+=48
    d.rectangle([620,430,820,470], fill=(210,205,195))             # pen
    stamp(d,w,h,when,stamped)
    im.save(os.path.join(OUT,path), quality=88); return path

def gym(when, path="gym_ok.jpg"):
    w,h = 900,650
    im = Image.new("RGB",(w,h),(58,58,64)); d = ImageDraw.Draw(im)
    d.rectangle([0,470,w,h], fill=(32,32,36))                      # rubber floor
    for x,lab in ((150,"20"),(430,"20"),(710,"10")):               # plates + bar
        d.ellipse([x-70,300,x+70,470], fill=(18,18,20), outline=(120,120,120), width=4)
        d.text((x-18,370), lab, fill=(200,200,200), font=font(34))
    d.rectangle([120,375,740,395], fill=(150,150,155))
    d.rectangle([60,120,300,300], fill=(45,45,50), outline=(90,90,95), width=3)
    d.text((80,150), "RACK 3", fill=(160,160,165), font=font(28))
    stamp(d,w,h,when)
    im.save(os.path.join(OUT,path), quality=88); return path

def ceiling(when, path="nothing.jpg"):
    w,h = 900,650
    im = Image.new("RGB",(w,h),(28,28,32)); d = ImageDraw.Draw(im)
    for i in range(0,h,90): d.line([(0,i),(w,i)], fill=(36,36,40), width=2)
    stamp(d,w,h,when)
    im.save(os.path.join(OUT,path), quality=88); return path

if __name__ == "__main__":
    now = datetime.now()
    print(" ", desk_with_maths(now))
    print(" ", gym(now))
    print(" ", ceiling(now))
    print(" ", desk_with_maths(now, stamped=False, path="work_nostamp.jpg"))
    from datetime import timedelta
    print(" ", desk_with_maths(now - timedelta(hours=3), path="work_stale.jpg"))
    print("written to", OUT)
