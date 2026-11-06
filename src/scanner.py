"""
Liquidation Scanner - Overseer Module
Monitors liquidation markets for target workstation hardware and for parts
(DDR4 ECC memory, Xeon Gold CPUs, enterprise SSDs).

Sources: eBay Browse API, Craigslist search pages (needs beautifulsoup4), and
RSS search feeds for other marketplaces (needs feedparser). Each matching
listing is emailed once per run of the process.
"""

import os
import sys
import smtplib
import time
import logging
import logging.handlers
import re
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path
from typing import List

import yaml
import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import feedparser
except Exception:
    feedparser = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path: str = None) -> dict:
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    cfg["alert"]["email_target"] = os.getenv("ALERT_EMAIL_TARGET", cfg["alert"]["email_target"])
    cfg["smtp"]["server"] = os.getenv("SMTP_SERVER", cfg["smtp"]["server"])
    cfg["smtp"]["port"] = int(os.getenv("SMTP_PORT", cfg["smtp"]["port"]))
    cfg["smtp"]["password"] = os.getenv("SMTP_PASSWORD", cfg["smtp"].get("password", ""))

    cfg["sources"]["ebay"]["api_key"] = os.getenv("EBAY_API_KEY", cfg["sources"]["ebay"].get("api_key", ""))
    cfg["sources"]["ebay"]["app_id"] = os.getenv("EBAY_APP_ID", cfg["sources"]["ebay"].get("app_id", ""))

    cfg["logging"]["level"] = os.getenv("LOG_LEVEL", cfg["logging"]["level"])
    return cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(cfg: dict) -> logging.Logger:
    logger = logging.getLogger("liquidation_scanner")
    logger.setLevel(getattr(logging, cfg["logging"]["level"].upper(), logging.INFO))
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    log_path = Path(cfg["logging"]["file"])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=cfg["logging"]["max_bytes"],
            backupCount=cfg["logging"]["backup_count"],
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except PermissionError:
        logger.warning(f"Cannot write to {log_path}, file logging disabled")
    return logger


# ---------------------------------------------------------------------------
# Data Sources
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    title: str
    price: float
    condition: str
    source_url: str
    source: str
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def parse_price(s: str) -> float:
    if not s:
        return 0.0
    m = re.findall(r'[\d,]+\.?\d*', str(s))
    if not m:
        return 0.0
    try:
        return float(m[-1].replace(',', ''))
    except Exception:
        return 0.0


def fetch_ebay(cfg: dict, logger: logging.Logger) -> List[Listing]:
    if not cfg["sources"].get("ebay", {}).get("enabled"):
        return []
    api_key = cfg["sources"]["ebay"].get("api_key", "")
    if not api_key:
        logger.warning("eBay source enabled but EBAY_API_KEY not set, skipping")
        return []
    listings = []
    for term in cfg["sources"]["ebay"].get("search_terms", []):
        try:
            resp = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Authorization": f"Bearer {api_key}"},
                params={
                    "q": term,
                    "category_ids": ",".join(str(c) for c in cfg["sources"]["ebay"].get("categories", [])),
                    "filter": f"price:[0..2000],priceCurrency:USD",
                    "sort": "price",
                    "limit": 20,
                },
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("itemSummaries", []):
                price_data = item.get("price", {})
                listings.append(Listing(
                    title=item.get("title", "Unknown"),
                    price=float(price_data.get("value", 0)),
                    condition=item.get("condition", "Unknown"),
                    source_url=item.get("itemWebUrl", ""),
                    source="ebay",
                    description=item.get("shortDescription", ""),
                ))
        except Exception as e:
            logger.error(f"eBay API error for '{term}': {e}")
    return listings


def fetch_craigslist(cfg: dict, logger: logging.Logger) -> List[Listing]:
    if not cfg["sources"].get("craigslist", {}).get("enabled"):
        return []
    if BeautifulSoup is None:
        logger.warning("craigslist is enabled but beautifulsoup4 is not installed; skipping it")
        return []
    listings = []
    regions = cfg["sources"]["craigslist"].get("regions", [])
    terms = cfg["sources"]["craigslist"].get("search_terms", [])
    if not regions or not terms:
        return []
    # Simple RSS/search approach via search API where possible; fallback to basic
    for region in regions:
        for term in terms:
            try:
                url = f"https://{region}.craigslist.org/search/sss?query={requests.utils.quote(term)}&sort=rel"
                r = requests.get(url, timeout=15, headers={"User-Agent": "liquidation-scanner/1.0"})
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                for row in soup.select("li.result-row")[:20]:
                    title_tag = row.select_one("a.result-title")
                    price_tag = row.select_one(".result-price")
                    link = title_tag["href"] if title_tag and title_tag.has_attr("href") else url
                    title = title_tag.text.strip() if title_tag else "Unknown"
                    price = parse_price(price_tag.text if price_tag else "0")
                    cond = row.select_one(".result-hood") or row.select_one(".result-tags")
                    cond_text = cond.text.strip() if cond else "Unknown"
                    listings.append(Listing(title=title, price=price, condition=cond_text, source_url=link, source="craigslist", description=""))
            except Exception as e:
                logger.debug(f"craigslist error {region}/{term}: {e}")
    return listings


def fetch_generic_rss(base_url: str, term: str, source_name: str, logger: logging.Logger) -> List[Listing]:
    """Read a marketplace's RSS search feed for one term."""
    listings = []
    try:
        q = requests.utils.quote(term)
        url = f"{base_url}/search?q={q}&format=rss"
        feed = feedparser.parse(url)
        for entry in feed.entries[:15]:
            title, summary = entry.get("title", ""), entry.get("summary", "")
            price = parse_price(title + " " + summary)
            listings.append(Listing(title=title, price=price, condition="Unknown", source_url=entry.get("link", url),
                                    source=source_name, description=summary))
    except Exception as e:
        logger.debug(f"{source_name} feed error for '{term}': {e}")
    return listings


def fetch_all_listings(cfg: dict, logger: logging.Logger) -> List[Listing]:
    all_listings = []
    all_listings.extend(fetch_ebay(cfg, logger))
    all_listings.extend(fetch_craigslist(cfg, logger))
    for src in ["govdeals", "liquidation", "bid4assets", "offerup"]:
        scfg = cfg["sources"].get(src, {})
        if scfg.get("enabled"):
            if feedparser is None:
                logger.warning(f"{src} is enabled but feedparser is not installed; skipping it")
                continue
            base = scfg.get("base_url", "")
            for term in scfg.get("search_terms", []):
                all_listings.extend(fetch_generic_rss(base, term, src, logger) if base else [])
    logger.info(f"Fetched {len(all_listings)} total listings from enabled sources")
    return all_listings

# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_listing(item: Listing, specs: dict, logger: logging.Logger) -> bool:
    title_lower = (item.title + " " + item.description).lower()
    cond_lower = item.condition.lower()
    parts_cfg = specs.get("parts", {})

    # DDR4 - prefer ECC/RDIMM/LRDIMM for P920 compatibility
    ddr4_terms = [t.lower() for t in parts_cfg.get("ddr4", {}).get("terms", [])]
    if any(t in title_lower for t in ddr4_terms):
        if re.search(r'\budimm\b.*non[- ]?ecc|\bnon[- ]?ecc\b.*\budimm\b', title_lower):
            pass
        elif 'udimm' in title_lower and 'ecc' not in title_lower and 'rdimm' not in title_lower and 'lrdimm' not in title_lower and 'registered' not in title_lower and 'load reduced' not in title_lower:
            pass
        else:
            pcfg = parts_cfg["ddr4"]
            if pcfg.get("min_total_usd", 0) <= item.price <= pcfg.get("max_total_usd", 10**9):
                logger.debug(f"Parts match (DDR4): {item.title} @ ${item.price}")
                return True

    # Xeon Gold
    xg_terms = [t.lower() for t in parts_cfg.get("xeon_gold", {}).get("terms", [])]
    if any(t in title_lower for t in xg_terms):
        pcfg = parts_cfg["xeon_gold"]
        if item.price <= pcfg.get("max_price_usd", 10**9):
            logger.debug(f"Parts match (Xeon Gold): {item.title} @ ${item.price}")
            return True

    # SSD
    ssd_terms = [t.lower() for t in parts_cfg.get("ssd", {}).get("terms", [])]
    if any(t in title_lower for t in ssd_terms):
        pcfg = parts_cfg["ssd"]
        if item.price <= pcfg.get("max_total_usd", 10**9):
            logger.debug(f"Parts match (SSD): {item.title} @ ${item.price}")
            return True

    # Full systems
    if item.price > specs.get("max_price_usd", 1200):
        return False
    if "parts only" in cond_lower and "tested" not in title_lower:
        logger.debug(f"Skipping parts-only untested: {item.title}")
        return False
    model_match = any(m.lower() in title_lower for m in specs.get("models", []))
    if not model_match:
        return False
    logger.debug(f"System match: {item.title} @ ${item.price}")
    return True

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def send_alert(item: Listing, cfg: dict, logger: logging.Logger) -> bool:
    msg = EmailMessage()
    msg["Subject"] = f"Liquidation Alert: {item.title} - ${item.price:.2f}"
    msg["From"] = cfg["alert"]["from_address"]
    msg["To"] = cfg["alert"]["email_target"]
    body = (
        f"Target Acquired:\n\n"
        f"Item: {item.title}\n"
        f"Price: ${item.price:.2f}\n"
        f"Source: {item.source_url}\n"
        f"Condition: {item.condition}\n"
        f"Platform: {item.source}\n\n"
        f"Consider pull-the-trigger."
    )
    msg.set_content(body)
    try:
        smtp_cfg = cfg["smtp"]
        with smtplib.SMTP(smtp_cfg["server"], smtp_cfg["port"]) as s:
            if smtp_cfg.get("use_tls"):
                s.starttls()
            if smtp_cfg.get("username") and smtp_cfg.get("password"):
                s.login(smtp_cfg["username"], smtp_cfg["password"])
            s.send_message(msg)
        logger.info(f"Alert dispatched: {item.title}")
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch alert for '{item.title}': {e}")
        return False


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def run_cycle(cfg: dict, logger: logging.Logger, alerted: set | None = None) -> int:
    """One sweep. `alerted` holds listing URLs already emailed, so a listing that
    stays up is not re-sent every poll."""
    alerted = set() if alerted is None else alerted
    logger.info("Overseer initiating liquidation sweep...")
    alerts_sent = 0
    try:
        listings = fetch_all_listings(cfg, logger)
    except Exception as e:
        logger.error(f"Failed to fetch listings: {e}")
        return 0
    for item in listings:
        if item.source_url in alerted:
            continue
        if evaluate_listing(item, cfg["target_specs"], logger):
            if send_alert(item, cfg, logger):
                alerted.add(item.source_url)
                alerts_sent += 1
    logger.info(f"Cycle complete. {alerts_sent} alerts sent from {len(listings)} listings.")
    return alerts_sent


def main():
    config_path = os.getenv("SCANNER_CONFIG", None)
    cfg = load_config(config_path)
    logger = setup_logging(cfg)
    logger.info("Liquidation Scanner starting up")
    alerted: set = set()
    while True:
        try:
            run_cycle(cfg, logger, alerted)
        except KeyboardInterrupt:
            logger.info("Shutdown requested, exiting cleanly")
            break
        except Exception as e:
            logger.error(f"Unhandled exception in cycle: {e}", exc_info=True)
        try:
            time.sleep(cfg["poll_interval_minutes"] * 60)
        except KeyboardInterrupt:
            logger.info("Shutdown requested during sleep, exiting cleanly")
            break
    logger.info("Liquidation Scanner shutdown complete")


if __name__ == "__main__":
    main()
