#!/usr/bin/env python3
"""
fttrailers_to_htt_xml.py

Downloads the FT Trailers TrailerOps feed and converts it into
HorseTrailerTrader-compatible XML.

Dealer:
- DMID: 880
- DLID: 31201
- Location: Palmyra, Missouri

Outputs:
- ft_trailers_htt_feed.xml
- ft_trailers_htt_debug.csv
"""

from __future__ import annotations

import csv
import html
import logging
import re
import sys
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.dom import minidom
import xml.etree.ElementTree as ET

import requests


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = "https://dealer.trailerops.com/integrations/fttrailers/trailer-trader"

DMID = "880"
DLID = "31201"

DEALER_NAME = "FT Trailers"
LOCATION_NAME = "Palmyra"
LOCATION_ADDRESS = "5864 Highway 24"
LOCATION_CITY = "Palmyra"
LOCATION_STATE = "MO"
LOCATION_ZIP = "63461"

OUTPUT_XML = "ft_trailers_htt_feed.xml"
OUTPUT_CSV = "ft_trailers_htt_debug.csv"
RAW_FEED = "ft_trailers_raw_feed.txt"

REQUEST_TIMEOUT = 60
MAX_IMAGES = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/plain,text/csv,*/*",
}

EXPECTED_COLUMNS = [
    "Unique ID",
    "Location ID",
    "Serial Number",
    "Stock Number",
    "Year",
    "Make",
    "Model",
    "Price",
    "Sale Price",
    "Description",
    "Category",
    "Class",
    "Condition",
    "Pull Type",
    "Length",
    "Width",
    "Height",
    "Weight",
    "Axles",
    "Color",
    "City",
    "State",
    "Zip",
    "Photo URLs",
]

CATEGORY_MAP = {
    "horse": "Horse Trailer",
    "horse - living quarters": "Horse Trailer",
    "livestock": "Stock Trailer",
    "dump": "Dump Trailer",
    "flatbed": "Equipment Trailer",
    "equipment": "Equipment Trailer",
    "utility": "Utility Trailer",
    "cargo": "Cargo Enclosed",
    "car hauler": "Car Trailer - Open",
}

MAKE_MAP = {
    "delta": "Delta Trailers",
    "hart trailers": "Hart Horse Trailers",
    "eby": "Eby Trailers",
    "lakota": "Lakota Trailers",
    "merhow": "Merhow",
    "exiss": "Exiss Trailers",
    "featherlite": "Featherlite Trailers",
    "swift built": "Swift Built",
    "twister": "Twister",
    "alumline": "Alum-Line",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# TEXT HELPERS
# ============================================================

class DescriptionParser(HTMLParser):
    BLOCK_TAGS = {
        "br", "p", "div", "li", "ul", "ol", "h1", "h2", "h3",
        "h4", "h5", "h6", "tr"
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""

    text = html.unescape(str(value))
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": "-",
        "\u00a0": " ",
        "\u2122": "",
        "\u00ae": "",
        "\u00a9": "",
        "\ufe0f": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Older importers are safest with printable ASCII plus line breaks.
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or 32 <= ord(ch) <= 126
    )

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_plain_text(value: str) -> str:
    parser = DescriptionParser()
    parser.feed(value or "")
    parser.close()
    return clean_text("".join(parser.parts))


def money_to_plain(value: str) -> str:
    raw = clean_text(value).replace("$", "").replace(",", "")
    if not raw:
        return ""

    try:
        amount = float(raw)
    except ValueError:
        return ""

    if amount <= 0:
        return ""

    return str(int(round(amount)))


def normalize_number(value: str) -> str:
    raw = clean_text(value).replace(",", "")
    if not raw:
        return ""

    try:
        number = float(raw)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not match:
            return ""
        number = float(match.group())

    if number <= 0:
        return ""

    if number.is_integer():
        return str(int(number))

    return f"{number:.2f}".rstrip("0").rstrip(".")


def feet_inches_to_decimal(value: str) -> str:
    """
    Examples:
      7'6" -> 7.5
      8'0" -> 8
      24"  -> 2
      7.6  -> 7.5 (TrailerOps commonly uses 7.6 to mean 7'6")
      8.5  -> 8.5
    """
    raw = clean_text(value).lower()
    if not raw:
        return ""

    feet_inches = re.search(
        r"(?P<feet>\d+(?:\.\d+)?)\s*['’]\s*(?P<inches>\d+(?:\.\d+)?)?\s*(?:[\"”]|in)?",
        raw,
    )
    if feet_inches:
        feet = float(feet_inches.group("feet"))
        inches = float(feet_inches.group("inches") or 0)
        total = feet + inches / 12
        return f"{total:.2f}".rstrip("0").rstrip(".")

    inches_only = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:\"|in|inches)\s*", raw)
    if inches_only:
        total = float(inches_only.group(1)) / 12
        return f"{total:.2f}".rstrip("0").rstrip(".")

    number = normalize_number(raw)
    if not number:
        return ""

    # TrailerOps uses values like 7.6 for 7 feet 6 inches.
    if re.fullmatch(r"\d+\.6", number):
        return str(float(number.split(".")[0]) + 0.5).rstrip("0").rstrip(".")

    return number


def add_child(parent: ET.Element, tag: str, value: Optional[str]) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = clean_text(value)
    return child


def prettify_xml(root: ET.Element) -> str:
    rough = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    return "\n".join(line for line in pretty.splitlines() if line.strip())


# ============================================================
# FIELD NORMALIZATION
# ============================================================

def normalize_category(raw_category: str, model: str, description: str) -> str:
    key = clean_text(raw_category).lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]

    blob = f"{key} {model} {description}".lower()

    if "horse" in blob:
        return "Horse Trailer"
    if any(term in blob for term in ["livestock", "stock combo", "stock trailer"]):
        return "Stock Trailer"
    if "dump" in blob:
        return "Dump Trailer"
    if any(term in blob for term in ["flatbed", "equipment"]):
        return "Equipment Trailer"
    if "utility" in blob:
        return "Utility Trailer"
    if any(term in blob for term in ["cargo", "enclosed"]):
        return "Cargo Enclosed"

    return ""


def normalize_make(raw_make: str) -> str:
    make = clean_text(raw_make)
    return MAKE_MAP.get(make.lower(), make)


def normalize_hitch(raw_hitch: str, description: str) -> str:
    raw = clean_text(raw_hitch).lower()
    blob = description.lower()

    if raw in {"gooseneck", "goose neck"} or "gooseneck" in blob:
        return "Gooseneck"
    if raw in {"bumper", "bumper pull", "bumperpull"}:
        return "Bumper Pull"
    if "bumper pull" in blob or "bumperpull" in blob:
        return "Bumper Pull"
    if "pintle" in raw or "pintle" in blob:
        return "Pintle Hook"

    return ""


def normalize_condition(raw_condition: str) -> str:
    raw = clean_text(raw_condition).lower()
    if raw == "new":
        return "New"
    if raw in {"used", "pre-owned", "preowned"}:
        return "Used"
    if raw == "demo":
        return "Demo"
    return ""


def derive_model(year: str, raw_make: str, mapped_make: str, description: str, category: str) -> str:
    model = clean_text(raw_make)
    if model:
        return model

    first_line = next(
        (clean_text(line) for line in description.splitlines() if clean_text(line)),
        "",
    )

    derived = first_line
    if year:
        derived = re.sub(rf"^\s*{re.escape(year)}\s+", "", derived, flags=re.I)

    for make_variant in sorted(
        {raw_make, mapped_make, mapped_make.replace(" Horse Trailers", " Trailers")},
        key=len,
        reverse=True,
    ):
        if make_variant:
            derived = re.sub(
                rf"^\s*{re.escape(make_variant)}\s+",
                "",
                derived,
                flags=re.I,
            )

    derived = clean_text(derived)
    if derived:
        return derived

    return clean_text(category) or "Trailer"


def infer_horses(model: str, description: str, htt_type: str) -> str:
    if htt_type not in {"Horse Trailer", "Stock Trailer"}:
        return ""

    blob = f"{model} {description}"
    patterns = [
        r"\b([1-9]|10)\s*[- ]?\s*horse(?:s)?\b",
        r"\b([1-9]|10)\s*H\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return match.group(1)

    return ""


def infer_living_quarters(raw_category: str, model: str, description: str) -> str:
    blob = f"{raw_category} {model} {description}".lower()
    return "true" if any(
        term in blob for term in ["living quarters", "living quarter", " lq ", " lq"]
    ) else "false"


def infer_lq_length(model: str, description: str) -> str:
    blob = f"{model}\n{description}"

    patterns = [
        r"\b(\d+(?:\.\d+)?)\s*['’]?\s*(?:lq|living quarter|living quarters)\b",
        r"\b(?:lq|living quarter|living quarters)\s*(\d+(?:\.\d+)?)\s*['’]?",
        r"\b(\d+(?:\.\d+)?)\s*['’]?\s*(?:shortwall|short wall)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return normalize_number(match.group(1))

    return ""


def infer_load_type(model: str, description: str, htt_type: str) -> str:
    if htt_type not in {"Horse Trailer", "Stock Trailer"}:
        return ""

    blob = f"{model} {description}".lower()
    if "straight load" in blob:
        return "Straight"
    if "reverse load" in blob:
        return "Reverse"
    if "head to head" in blob or "head-to-head" in blob:
        return "HeadtoHead"
    if "slant" in blob:
        return "Slant"
    if any(term in blob for term in ["stock combo", "livestock", "stock trailer"]):
        return "Stock"

    return ""


def infer_construction(make: str, description: str) -> str:
    blob = f"{make} {description}".lower()

    if "steel frame" in blob and "aluminum" in blob:
        return "Steel Frame +AL"
    if "galvanized steel" in blob:
        return "Galvanized Steel"
    if "aluminum" in blob or "alumline" in blob or "alum-line" in blob:
        return "Aluminum"
    if "steel" in blob:
        return "Steel"

    return ""


def infer_slideouts(description: str) -> str:
    match = re.search(
        r"\b([1-4])\s*(?:slide|slides|slideout|slideouts|slide out|slide outs)\b",
        description,
        flags=re.I,
    )
    if match:
        return match.group(1)

    if re.search(r"\bslide\s*out\b|\bslideout\b", description, flags=re.I):
        return "1"

    return ""


def boolean_from_keywords(description: str, keywords: List[str]) -> str:
    blob = description.lower()
    return "true" if any(keyword.lower() in blob for keyword in keywords) else "false"


def collect_images(raw_value: str) -> List[str]:
    images: List[str] = []
    seen = set()

    for item in (raw_value or "").split(","):
        url = clean_text(item)
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue

        seen.add(url)
        images.append(url)

        if len(images) >= MAX_IMAGES:
            break

    return images


def get_final_price(regular_price: str, sale_price: str) -> Tuple[str, str, str]:
    regular = money_to_plain(regular_price)
    sale = money_to_plain(sale_price)
    return regular, sale, sale or regular


# ============================================================
# DOWNLOAD AND PARSE
# ============================================================

def download_feed() -> str:
    response = requests.get(
        SOURCE_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    logging.info("HTTP %s | %s", response.status_code, SOURCE_URL)
    response.raise_for_status()

    response.encoding = response.encoding or "utf-8"
    text = response.text.lstrip("\ufeff").strip()

    if not text:
        raise ValueError("TrailerOps returned an empty feed.")

    Path(RAW_FEED).write_text(text, encoding="utf-8")
    return text


def parse_feed(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(StringIO(text), delimiter="|")

    if not reader.fieldnames:
        raise ValueError("The feed does not contain a header row.")

    actual = [clean_text(name) for name in reader.fieldnames]
    missing = [column for column in EXPECTED_COLUMNS if column not in actual]
    if missing:
        raise ValueError(
            "TrailerOps feed format changed. Missing columns: "
            + ", ".join(missing)
        )

    rows: List[Dict[str, str]] = []

    for line_number, row in enumerate(reader, start=2):
        normalized = {
            clean_text(key): clean_text(value)
            for key, value in row.items()
            if key is not None
        }

        if not any(normalized.values()):
            continue

        if None in row:
            raise ValueError(
                f"Line {line_number} contains more than 24 fields. "
                "The source format may have changed."
            )

        normalized["_line_number"] = str(line_number)
        rows.append(normalized)

    return rows


# ============================================================
# XML BUILD
# ============================================================

def build_item(
    parent: ET.Element,
    source: Dict[str, str],
) -> Dict[str, str]:

    source_id = source["Unique ID"]
    stock = source["Stock Number"]
    year = source["Year"]
    raw_make = source["Make"]
    make = normalize_make(raw_make)
    raw_model = source["Model"]
    description = html_to_plain_text(source["Description"])
    raw_category = source["Category"]

    htt_type = normalize_category(raw_category, raw_model, description)
    model = derive_model(year, raw_model, make, description, htt_type)

    regular_price, sale_price, final_price = get_final_price(
        source["Price"],
        source["Sale Price"],
    )

    condition = normalize_condition(source["Condition"])
    hitch = normalize_hitch(source["Pull Type"], description)

    length = feet_inches_to_decimal(source["Length"])
    width = feet_inches_to_decimal(source["Width"])
    height = feet_inches_to_decimal(source["Height"])
    weight = normalize_number(source["Weight"])
    axles = normalize_number(source["Axles"])

    horses = infer_horses(model, description, htt_type)
    living_quarters = infer_living_quarters(
        raw_category,
        model,
        description,
    )
    lq_length = infer_lq_length(model, description)
    load_type = infer_load_type(model, description, htt_type)
    construction = infer_construction(make, description)
    images = collect_images(source["Photo URLs"])

    warnings: List[str] = []

    required_values = {
        "type": htt_type,
        "stocknumber": stock,
        "year": year,
        "condition": condition,
        "make": make,
        "model": model,
        "hitch": hitch,
        "length": length,
        "width": width,
        "height": height,
        "price": final_price,
        "description": description,
    }

    for field, value in required_values.items():
        if not value:
            warnings.append(f"missing {field}")

    if not images:
        warnings.append("no images")

    item = ET.SubElement(parent, "inventoryitem")

    add_child(item, "type", htt_type)
    add_child(item, "stocknumber", stock)
    add_child(item, "year", year)
    add_child(item, "condition", condition)
    add_child(item, "make", make)
    add_child(item, "model", model)
    add_child(item, "status", "Available")
    add_child(item, "onorder", "false")

    add_child(item, "hitch", hitch)
    add_child(item, "horses", horses)
    add_child(item, "axles", axles)
    add_child(item, "length", length)
    add_child(item, "width", width)
    add_child(item, "height", height)
    add_child(item, "price", final_price)
    add_child(item, "slideouts", infer_slideouts(description))
    add_child(item, "description", description)

    add_child(item, "lqlength", lq_length)
    add_child(item, "loadtype", load_type)
    add_child(item, "construction", construction)
    add_child(item, "emptyweight", weight)
    add_child(item, "gtwr", "")
    add_child(item, "vin", source["Serial Number"])

    add_child(item, "livingquarters", living_quarters)
    add_child(
        item,
        "midtack",
        boolean_from_keywords(description, ["mid tack", "midtack"]),
    )
    add_child(
        item,
        "manger",
        boolean_from_keywords(description, ["manger"]),
    )
    add_child(
        item,
        "rearramp",
        boolean_from_keywords(description, ["rear ramp", "ramp door"]),
    )
    add_child(
        item,
        "sideramp",
        boolean_from_keywords(description, ["side ramp", "side load ramp"]),
    )

    images_node = ET.SubElement(item, "images")
    for index, image_url in enumerate(images, start=1):
        add_child(images_node, f"image{index}", image_url)

    return {
        "source_id": source_id,
        "source_line": source["_line_number"],
        "dmid": DMID,
        "dlid": DLID,
        "stock": stock,
        "vin": source["Serial Number"],
        "year": year,
        "raw_make": raw_make,
        "make": make,
        "raw_model": raw_model,
        "model": model,
        "raw_category": raw_category,
        "htt_type": htt_type,
        "condition": condition,
        "hitch": hitch,
        "length": length,
        "width": width,
        "height": height,
        "axles": axles,
        "horses": horses,
        "living_quarters": living_quarters,
        "lq_length": lq_length,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "final_price": final_price,
        "image_count": str(len(images)),
        "warnings": "; ".join(warnings),
    }


def build_xml(
    source_rows: List[Dict[str, str]],
) -> Tuple[ET.Element, List[Dict[str, str]]]:

    root = ET.Element("feedimport")

    dealer = ET.SubElement(root, "dealer")
    add_child(dealer, "dealerid", DMID)
    add_child(dealer, "dealername", DEALER_NAME)

    location = ET.SubElement(dealer, "location")
    add_child(location, "locationid", DLID)
    add_child(location, "locationname", LOCATION_NAME)
    add_child(location, "locationaddress", LOCATION_ADDRESS)
    add_child(location, "locationcity", LOCATION_CITY)
    add_child(location, "locationstate", LOCATION_STATE)
    add_child(location, "locationzip", LOCATION_ZIP)

    debug_rows: List[Dict[str, str]] = []
    seen_stocks = set()

    for source in source_rows:
        stock = clean_text(source["Stock Number"])

        if not stock:
            logging.warning(
                "Skipping line %s because it has no stock number.",
                source["_line_number"],
            )
            continue

        if stock in seen_stocks:
            logging.warning("Skipping duplicate stock number: %s", stock)
            continue

        seen_stocks.add(stock)
        debug_rows.append(build_item(location, source))

    return root, debug_rows


def write_debug_csv(rows: List[Dict[str, str]]) -> None:
    fieldnames = [
        "source_id",
        "source_line",
        "dmid",
        "dlid",
        "stock",
        "vin",
        "year",
        "raw_make",
        "make",
        "raw_model",
        "model",
        "raw_category",
        "htt_type",
        "condition",
        "hitch",
        "length",
        "width",
        "height",
        "axles",
        "horses",
        "living_quarters",
        "lq_length",
        "regular_price",
        "sale_price",
        "final_price",
        "image_count",
        "warnings",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    logging.info("Starting FT Trailers to HTT conversion")
    logging.info("DMID: %s | DLID: %s", DMID, DLID)

    try:
        feed_text = download_feed()
        source_rows = parse_feed(feed_text)
        logging.info("TrailerOps records found: %s", len(source_rows))

        root, debug_rows = build_xml(source_rows)

        Path(OUTPUT_XML).write_text(
            prettify_xml(root),
            encoding="utf-8",
        )
        write_debug_csv(debug_rows)

    except requests.RequestException as exc:
        logging.exception("Could not download the TrailerOps feed: %s", exc)
        return 1
    except Exception as exc:
        logging.exception("Conversion failed: %s", exc)
        return 1

    warning_rows = [row for row in debug_rows if row["warnings"]]

    logging.info("Units written: %s", len(debug_rows))
    logging.info("Units with warnings: %s", len(warning_rows))
    logging.info("XML output: %s", OUTPUT_XML)
    logging.info("Debug output: %s", OUTPUT_CSV)
    logging.info("Raw source saved: %s", RAW_FEED)

    # Confirm the test unit's pricing when it is present.
    test_unit = next(
        (row for row in debug_rows if row["stock"] == "10077C"),
        None,
    )
    if test_unit:
        logging.info(
            "Stock 10077C | regular=%s | sale=%s | final=%s",
            test_unit["regular_price"],
            test_unit["sale_price"],
            test_unit["final_price"],
        )

    logging.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())