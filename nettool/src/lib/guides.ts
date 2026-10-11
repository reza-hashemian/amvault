// The command guides are plain JSON in /guides so they can be edited without
// touching code, and so the device panels can later build commands from the
// same formulas the guide shows.
import type { Guide, GuideItem, Platform } from "./types";

const files = import.meta.glob<Guide>("../../guides/*.json", { eager: true, import: "default" });

const ORDER: Platform[] = ["cisco_ios", "mikrotik", "linux", "windows"];

export const guides: Guide[] = Object.values(files).sort(
  (a, b) => ORDER.indexOf(a.platform) - ORDER.indexOf(b.platform),
);

export function guideFor(platform: Platform): Guide | undefined {
  return guides.find((guide) => guide.platform === platform);
}

function matches(item: GuideItem, words: string[]): boolean {
  const text = [item.title, item.summary, item.note ?? "", ...item.syntax, ...item.example]
    .join(" ")
    .toLowerCase();
  return words.every((word) => text.includes(word));
}

/** The guide's groups narrowed to the items matching every word of `query`. */
export function searchGuide(guide: Guide, query: string): Guide["groups"] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return guide.groups;
  return guide.groups
    .map((group) => ({ ...group, items: group.items.filter((item) => matches(item, words)) }))
    .filter((group) => group.items.length > 0);
}
