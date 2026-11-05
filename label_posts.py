"""Label a posts CSV on your own computer.

    python label_posts.py posts.csv labelled.csv

Fills in blank format/hook/offer columns from each post's caption.
With MODEL_API_KEY set it uses Meta AI (or Claude with ANTHROPIC_API_KEY); otherwise the free keyword rules.
Posts it isn't sure about are listed so you can check them by hand.
"""
import csv
import sys

from paysignal.labeler import label_posts, make_labeler
from paysignal.organic import load_posts_csv


def main(src: str, dst: str) -> None:
    with open(src, encoding="utf-8-sig") as f:
        text = f.read()
    posts, errors = load_posts_csv(text)
    for e in errors:
        print("skipped", e)
    summary = label_posts(posts, make_labeler())
    print(f"labelled {summary['labelled']} posts using {summary['source'] or 'nothing (all already labelled)'}")
    if summary["low_confidence"]:
        print("please check these by hand:", ", ".join(summary["low_confidence"]))

    by_id = {p.post_id: p for p in posts}
    rows = list(csv.DictReader(text.splitlines()))
    fields = list(rows[0].keys()) if rows else []
    for col in ("format", "hook", "offer", "label_source"):
        if col not in fields:
            fields.append(col)
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            p = by_id.get((row.get("post_id") or "").strip())
            if p:
                row.update(format=p.format, hook=p.hook, offer=p.offer, label_source=p.label_source)
            w.writerow(row)
    print("saved", dst)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
