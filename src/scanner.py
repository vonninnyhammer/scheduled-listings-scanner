"""
Liquidation Scanner - Overseer Module
Monitors liquidation markets for target workstation hardware.
"""

import os
import sys
import smtplib
import time
import logging
import logging.handlers
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path

import yaml
import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path: str = None) -> dict:
    """Load YAML config with env var overrides."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "settings.yaml"

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    # Env var overrides (secrets and runtime tweaks)
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
    """Configure rotating file + stdout logging."""
    logger = logging.getLogger("liquidation_scanner")
    logger.setLevel(getattr(logging, cfg["logging"]["level"].upper(), logging.INFO))

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Rotating file handler (if path is writable)
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
    """Normalized listing object from any source."""
    title: str
    price: float
    condition: str
    source_url: str
    source: str
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_ebay(cfg: dict, logger: logging.Logger) -> list[Listing]:
    """
    Query eBay Browse API for matching listings.
    Requires EBAY_API_KEY and EBAY_APP_ID env vars.
    """
    if not cfg["sources"]["ebay"]["enabled"]:
        return []

    api_key = cfg["sources"]["ebay"]["api_key"]
    if not api_key:
        logger.warning("eBay source enabled but EBAY_API_KEY not set, skipping")
        return []

    listings = []
    for term in cfg["sources"]["ebay"]["search_terms"]:
        try:
            resp = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Authorization": f"Bearer {api_key}"},
                params={
                    "q": term,
                    "category_ids": ",".join(cfg["sources"]["ebay"]["categories"]),
                    "filter": f"price:[0..{cfg['target_specs']['max_price_usd']}],priceCurrency:USD",
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
            logger.debug(f"eBay: {len(resp.json().get('itemSummaries', []))} results for '{term}'")
        except requests.RequestException as e:
            logger.error(f"eBay API error for '{term}': {e}")

    return listings


def fetch_all_listings(cfg: dict, logger: logging.Logger) -> list[Listing]:
    """Aggregate listings from all enabled sources."""
    all_listings = []
    all_listings.extend(fetch_ebay(cfg, logger))
    logger.info(f"Fetched {len(all_listings)} total listings from enabled sources")
    return all_listings


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_listing(item: Listing, specs: dict, logger: logging.Logger) -> bool:
    """Check if listing meets target hardware and price criteria."""
    # Price gate
    if item.price > specs["max_price_usd"]:
        return False

    # Condition gate - skip parts-only unless tested
    cond_lower = item.condition.lower()
    if "parts only" in cond_lower and "tested" not in item.description.lower():
        logger.debug(f"Skipping parts-only untested: {item.title}")
        return False

    # Model match (fuzzy - check if any target model appears in title)
    title_lower = item.title.lower()
    model_match = any(m.lower() in title_lower for m in specs["models"])
    if not model_match:
        return False

    logger.debug(f"Listing passed evaluation: {item.title} @ ${item.price}")
    return True


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def send_alert(item: Listing, cfg: dict, logger: logging.Logger) -> bool:
    """Dispatch alert email. Returns True on success."""
    msg = EmailMessage()
    msg["Subject"] = f"Cheap Iron Alert: {item.title} - ${item.price}"
    msg["From"] = cfg["alert"]["from_address"]
    msg["To"] = cfg["alert"]["email_target"]

    body = (
        f"Target Acquired:\n\n"
        f"Model: {item.title}\n"
        f"Price: ${item.price:.2f}\n"
        f"Source: {item.source_url}\n"
        f"Condition: {item.condition}\n"
        f"Platform: {item.source}\n\n"
        f"Deploying Hermes execution handshake."
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

def run_cycle(cfg: dict, logger: logging.Logger) -> int:
    """Execute one surveillance cycle. Returns count of alerts sent."""
    logger.info("Overseer initiating liquidation sweep...")
    alerts_sent = 0

    try:
        listings = fetch_all_listings(cfg, logger)
    except Exception as e:
        logger.error(f"Failed to fetch listings: {e}")
        return 0

    for item in listings:
        if evaluate_listing(item, cfg["target_specs"], logger):
            if send_alert(item, cfg, logger):
                alerts_sent += 1

    logger.info(f"Cycle complete. {alerts_sent} alerts sent from {len(listings)} listings.")
    return alerts_sent


def main():
    """Entry point with resilient main loop."""
    config_path = os.getenv("SCANNER_CONFIG", None)
    cfg = load_config(config_path)
    logger = setup_logging(cfg)

    logger.info("Liquidation Scanner starting up")
    logger.info(f"Monitoring targets: {cfg['target_specs']['models']}")
    logger.info(f"Max price: ${cfg['target_specs']['max_price_usd']:.2f}")
    logger.info(f"Poll interval: {cfg['poll_interval_minutes']} minutes")

    while True:
        try:
            run_cycle(cfg, logger)
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
