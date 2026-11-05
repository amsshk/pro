"""Harder captions: indirect wording, slang, mixed language, few obvious keywords.
Used to measure how often the free rules labeller is right on messy real-world captions."""
HARD = [
    ("Can't believe this is the same person 😳 6 weeks apart", "REELS", "instagram", "before_after", "none"),
    ("Week 1 vs week 8. No filter.", "CAROUSEL_ALBUM", "instagram", "before_after", "none"),
    ("She walked in nervous and left glowing. Thank you Maryam for trusting us 🤍", "REELS", "instagram", "testimonial", "none"),
    ("'I wish I had come here years ago' - this made our day", "STORY", "instagram", "testimonial", "none"),
    ("كلام عميلاتنا يفرحنا 🥹", "STORY", "instagram", "testimonial", "none"),
    ("Stop washing your face with hot water. Here's why 👇", "REELS", "instagram", "tutorial", "none"),
    ("Save this for your next appointment 📌", "CAROUSEL_ALBUM", "instagram", "tutorial", "none"),
    ("الغلطة اللي الكل يسويها مع واقي الشمس", "REELS", "instagram", "tutorial", "none"),
    ("Nobody: ... Me at 2am adding everything to cart", "", "tiktok", "trend_meme", "none"),
    ("Wait for it 😭", "REELS", "instagram", "trend_meme", "none"),
    ("5am setup before the doors open ☕", "STORY", "instagram", "behind_scenes", "none"),
    ("This is where the magic happens 🧪", "REELS", "instagram", "behind_scenes", "none"),
    ("It's finally here. Link in bio.", "IMAGE", "instagram", "product_showcase", "none"),
    ("Only 48 hours left, prices go back up on Sunday", "STORY", "instagram", "other", "discount"),
    ("Book 2 friends, the third comes on us 💕", "IMAGE", "instagram", "other", "bundle"),
    ("Every booking this week gets a little surprise from us 🎀", "STORY", "instagram", "other", "free_gift"),
]
