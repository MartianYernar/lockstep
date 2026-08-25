# What this system knows about you, and who else sees it

You asked about background geolocation from photos. It's a real risk, not
paranoia, so here is the honest accounting rather than reassurance.

---

## The claim you heard is true

Modern vision models can place a photo from its background with unsettling
accuracy — architecture, window patterns, roof lines, signage, vegetation,
kerb style, the angle of the sun. Dedicated tools exist that do only this. You
don't need a landmark in frame; a residential skyline is often enough to narrow
things to a neighbourhood.

**I tested it on this system's own pipeline.** I generated a photo of monkey
bars with a city skyline behind, sent it to Gemini at five different sizes, and
asked what it saw:

| Size | What the model reported |
|---|---|
| 3000px | "outdoor monkey bars in front of city buildings" |
| 1600px | "monkey bars in front of city buildings" |
| 1024px | "monkey bars set against a skyline of buildings" |
| 768px | "outdoor monkey bars in front of city buildings" |
| 512px | "monkey bars set against a background of high-rise buildings" |

Shrinking the image to a sixth of its size did **not** remove the skyline. So
downscaling is not a privacy fix, and I'm not going to sell it as one.

---

## What the code does about it

Every photo, before it goes anywhere:

- **Metadata stripped.** Re-encoded through Pillow with no EXIF block, so GPS
  coordinates, device serial and capture settings are gone. If the original
  carried GPS, the bot tells you so — because every *other* app you send photos
  to keeps it.
- **Downscaled to 1024px.** This doesn't hide a skyline, but it does remove the
  fine detail that reads street signs, door numbers, and licence plates. It also
  cuts your Gemini quota use. Verified: the timestamp still reads at 512px, so
  there's no accuracy cost.
- **Stored as a 320px thumbnail, not the full photo** — enough to remember what
  you sent, not much use to anyone who gets hold of the server.
- **Deleted after 7 days.** Configurable; `store_photos: none` keeps nothing but
  the verdict.
- **A framing reminder** on outdoor blocks, because framing is the only thing
  that actually works.

---

## Who sees your photos

Be clear-eyed about this. Three parties, and only one is optional.

**1. Google.** Every photo goes to the Gemini API to be checked. On the **free
tier, Google may retain your prompts and use them to improve their products,
including human review.** That is the actual cost of the free tier. You are
sending photos of yourself, your room, and your neighbourhood under those terms.

If that bothers you — and it reasonably might — the options are:
- **Pay for the API.** Paid-tier data isn't used for training. It's cents per
  month at eight photos a day. It's the cleanest fix and barely counts as money.
- **Frame tighter.** Costs nothing, see below.
- **Ask me for timestamp-only mode.** I can add local OCR that reads the clock
  on your server without the photo ever leaving it. You'd lose the "is this
  actually the gym" check and keep the "is this actually now" check. Weaker, but
  nothing goes to Google.

**2. Telegram.** Photos pass through and sit in your chat history. Normal, but
it's a copy you don't control. Anyone with your bot token can read the whole
conversation — which is why the token belongs in `config.yaml` and nowhere else.

**3. Your server.** Thumbnails only, deleted after a week.

Your father sees **numbers, never photos**. His nightly report is a count of
checkpoints hit and missed. That was deliberate and it isn't configurable.

---

## The part that actually protects you

**Frame the shot so the background isn't in it.**

- Point the camera **down at the equipment** or **close on yourself**, not out at
  the view
- Shoot with your back to open ground, so the frame fills with grass or the bars
- A tight shot of your hands on the bar with the timestamp in the corner passes
  the check perfectly and shows a stranger nothing

The check needs to see *the activity* and *the clock*. It does not need to see
where you are. A photo cropped to just the equipment satisfies it completely.

Also, once: **turn off location tagging in your camera app.** The server strips
it, but that setting affects every photo you take, including the ones you send
to people who don't strip anything.

---

## What I can't fix

If you photograph yourself outdoors and send it to a vision model, that model
sees your surroundings. There is no setting that makes that untrue. The choices
are framing, paying for a non-retaining tier, or not sending outdoor photos to a
model at all — and you should pick knowingly rather than assume the software
handled it.
