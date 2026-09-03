"""Pure tenant-product matching helpers shared by POS services."""
import difflib
import re
import unicodedata


def normalize_product_query(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def rank_product_matches(query: str, products, *, limit: int = 5):
    needle = normalize_product_query(query)
    if not needle:
        return []
    needle_tokens = set(needle.split())
    ranked = []
    for product in products:
        sku = normalize_product_query(product["sku"])
        name = normalize_product_query(product["name"])
        barcode_value = product.get("barcode") if hasattr(product, "get") else product["barcode"]
        barcode = normalize_product_query(barcode_value)
        exact = needle in ({sku, name, barcode} - {""})
        if exact:
            score = 1.0
        elif needle_tokens and needle_tokens.issubset(set(name.split())):
            score = 0.92
        elif needle in name or name in needle:
            score = 0.86
        else:
            score = max(
                difflib.SequenceMatcher(None, needle, name).ratio(),
                difflib.SequenceMatcher(None, needle, sku).ratio(),
            )
        if score >= 0.62:
            ranked.append({"product": product, "score": score, "exact": exact})
    ranked.sort(key=lambda item: (-item["score"], str(item["product"]["name"]).casefold()))
    return ranked[:max(1, min(int(limit), 10))]


def choose_product_match(query: str, products):
    ranked = rank_product_matches(query, products)
    if not ranked:
        return None, []
    exact = [item for item in ranked if item["exact"]]
    if len(exact) == 1:
        return exact[0]["product"], []
    top = ranked[0]
    second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if top["score"] >= 0.82 and (len(ranked) == 1 or top["score"] - second_score >= 0.12):
        return top["product"], []
    return None, [item["product"] for item in ranked]
