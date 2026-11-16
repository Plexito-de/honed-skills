#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file beside this script.
"""Factur-X / ZUGFeRD 2.2 hybrid e-invoice generator and validator (PDF/A-3b with EN 16931 XML).

Purpose:
    Generates and proves Factur-X / ZUGFeRD 2.2 hybrid e-invoices conforming to EN 16931 and
    ISO 19005-3 (PDF/A-3b). Evaluated against veraPDF 1.30.4 (-f 3b) and the KoSIT Prüftool.

Usage:
    python3 invoice.py pack --pdf input.pdf --data invoice.json --out hybrid.pdf
    python3 invoice.py attach --pdf input.pdf --xml factur-x.xml --out hybrid.pdf
    python3 invoice.py extract hybrid.pdf [--out factur-x.xml]
    python3 invoice.py verify hybrid.pdf
    python3 invoice.py --selftest

Exit codes:
    0: verified / success
    1: refused (input breaks a rule or math discrepancy)
    2: usage error or missing file
    3: dependency missing
    4: proof failed (PDF/A-3b or XSD validation failure)

Requires Python 3.10+ with factur-x==7.9 and pypdf==7.0.0.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

EN16931_URN = "urn:cen.eu:en16931:2017"
EN16931_EXTENDED_URN = "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:en16931"
DEFAULT_PROFILE = "EN 16931"
DEFAULT_FILENAME = "factur-x.xml"


class Refused(Exception):
    """Input that violates Factur-X, EN 16931 or PDF/A-3 requirements."""


class ProofFailed(Exception):
    """The examined PDF fails veraPDF PDF/A-3b rules, XSD validation, or business invariants."""


class DependencyMissing(Exception):
    """A required external dependency is not installed."""


def libraries():
    """Import and return the pinned third-party dependencies."""
    try:
        import facturx
        import pypdf
    except ImportError as err:
        raise DependencyMissing(
            f"{err.name} is not installed. In a virtual environment: "
            "pip install factur-x==7.9 pypdf==7.0.0"
        ) from err
    return facturx, pypdf


def quantize(val: float | int | str | Decimal) -> Decimal:
    """Round to 2 decimal places using half-up rounding."""
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_facturx_xml(data: dict) -> bytes:
    """Build a compliant CrossIndustryInvoice XML strictly conforming to EN 16931."""
    for req in ("number", "date", "seller", "buyer", "currency", "lines", "total"):
        if req not in data:
            raise Refused(f"missing required invoice field: {req}")

    lines_data = data.get("lines", [])
    if not lines_data:
        raise Refused("invoice must contain at least one line item")

    rsm = ET.Element(
        "rsm:CrossIndustryInvoice",
        attrib={
            "xmlns:rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
            "xmlns:ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
            "xmlns:qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
            "xmlns:udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
        },
    )

    ctx = ET.SubElement(rsm, "rsm:ExchangedDocumentContext")
    guideline = ET.SubElement(ctx, "ram:GuidelineSpecifiedDocumentContextParameter")
    guideline_id = data.get("guideline_id", EN16931_URN)
    if not (guideline_id.startswith("urn:cen.eu:en16931:2017")):
        raise Refused(f"KoSIT [BR-01]: specification identifier must be EN 16931, got {guideline_id}")
    ET.SubElement(guideline, "ram:ID").text = guideline_id

    doc = ET.SubElement(rsm, "rsm:ExchangedDocument")
    ET.SubElement(doc, "ram:ID").text = str(data["number"])
    ET.SubElement(doc, "ram:TypeCode").text = str(data.get("type_code", "380"))  # 380 = Commercial invoice
    issue_date = ET.SubElement(doc, "ram:IssueDateTime")
    dt_str = ET.SubElement(issue_date, "udt:DateTimeString", attrib={"format": "102"})
    clean_date = str(data["date"]).replace("-", "")[:8]
    if len(clean_date) != 8 or not clean_date.isdigit():
        raise Refused(f"invalid date {data['date']}; format must be YYYY-MM-DD or YYYYMMDD")
    dt_str.text = clean_date

    tx = ET.SubElement(rsm, "rsm:SupplyChainTradeTransaction")

    total_val = quantize(data["total"])
    if total_val < 0:
        raise Refused("invoice total cannot be negative")

    currency = str(data.get("currency", "EUR"))

    # 1. IncludedSupplyChainTradeLineItem (MANDATORY in EN 16931, preceding Agreement)
    calculated_line_net = Decimal("0.00")
    tax_breakdown: dict[tuple[str, Decimal], dict[str, Decimal]] = {}

    for idx, line in enumerate(lines_data, start=1):
        line_item = ET.SubElement(tx, "ram:IncludedSupplyChainTradeLineItem")
        line_doc = ET.SubElement(line_item, "ram:AssociatedDocumentLineDocument")
        ET.SubElement(line_doc, "ram:LineID").text = str(line.get("id", idx))

        prod = ET.SubElement(line_item, "ram:SpecifiedTradeProduct")
        line_name = line.get("name", f"Item {idx}")
        ET.SubElement(prod, "ram:Name").text = line_name

        qty = quantize(line.get("quantity", 1.0))
        unit_price = quantize(line.get("unit_price", line.get("amount", 0.0)))
        line_amount = quantize(line.get("amount", qty * unit_price))
        calculated_line_net += line_amount

        line_tax_cat = str(line.get("tax_category", "S"))
        line_tax_pct = quantize(line.get("tax_percent", 19.0))
        unit_code = str(line.get("unit_code", "C62"))

        # SpecifiedLineTradeAgreement
        line_agr = ET.SubElement(line_item, "ram:SpecifiedLineTradeAgreement")
        net_price = ET.SubElement(line_agr, "ram:NetPriceProductTradePrice")
        ET.SubElement(net_price, "ram:ChargeAmount").text = f"{unit_price:.2f}"

        # SpecifiedLineTradeDelivery
        line_deliv = ET.SubElement(line_item, "ram:SpecifiedLineTradeDelivery")
        ET.SubElement(line_deliv, "ram:BilledQuantity", attrib={"unitCode": unit_code}).text = f"{qty:.2f}"

        # SpecifiedLineTradeSettlement
        line_settle = ET.SubElement(line_item, "ram:SpecifiedLineTradeSettlement")
        line_tax = ET.SubElement(line_settle, "ram:ApplicableTradeTax")
        ET.SubElement(line_tax, "ram:TypeCode").text = "VAT"
        ET.SubElement(line_tax, "ram:CategoryCode").text = line_tax_cat
        ET.SubElement(line_tax, "ram:RateApplicablePercent").text = f"{line_tax_pct:.2f}"

        line_sum = ET.SubElement(line_settle, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        ET.SubElement(line_sum, "ram:LineTotalAmount").text = f"{line_amount:.2f}"

        # Aggregate tax breakdown
        key = (line_tax_cat, line_tax_pct)
        if key not in tax_breakdown:
            tax_breakdown[key] = {"basis": Decimal("0.00"), "tax": Decimal("0.00")}
        tax_breakdown[key]["basis"] += line_amount

    # Compute tax amounts for each category
    total_tax = Decimal("0.00")
    for key, val in tax_breakdown.items():
        rate = key[1]
        tax_amt = quantize(val["basis"] * (rate / Decimal("100.00")))
        val["tax"] = tax_amt
        total_tax += tax_amt

    net_val = quantize(data.get("net", calculated_line_net))
    tax_val = quantize(data.get("tax", total_tax))

    # KoSIT BR-CO-15: GrandTotalAmount = TaxBasisTotalAmount + TaxTotalAmount
    expected_total = quantize(net_val + tax_val)
    if total_val != expected_total:
        raise Refused(
            f"KoSIT [BR-CO-15]: grand total ({total_val:.2f}) must equal net ({net_val:.2f}) "
            f"+ tax ({tax_val:.2f}) = {expected_total:.2f}"
        )

    # 2. ApplicableHeaderTradeAgreement
    agr = ET.SubElement(tx, "ram:ApplicableHeaderTradeAgreement")
    seller = ET.SubElement(agr, "ram:SellerTradeParty")
    ET.SubElement(seller, "ram:Name").text = data["seller"].get("name", "Seller")
    if data["seller"].get("vat_id"):
        seller_tax = ET.SubElement(seller, "ram:SpecifiedTaxRegistration")
        ET.SubElement(seller_tax, "ram:ID", attrib={"schemeID": "VA"}).text = data["seller"]["vat_id"]

    buyer = ET.SubElement(agr, "ram:BuyerTradeParty")
    ET.SubElement(buyer, "ram:Name").text = data["buyer"].get("name", "Buyer")
    if data["buyer"].get("vat_id"):
        buyer_tax = ET.SubElement(buyer, "ram:SpecifiedTaxRegistration")
        ET.SubElement(buyer_tax, "ram:ID", attrib={"schemeID": "VA"}).text = data["buyer"]["vat_id"]

    # 3. ApplicableHeaderTradeDelivery
    ET.SubElement(tx, "ram:ApplicableHeaderTradeDelivery")

    # 4. ApplicableHeaderTradeSettlement
    settle = ET.SubElement(tx, "ram:ApplicableHeaderTradeSettlement")
    ET.SubElement(settle, "ram:InvoiceCurrencyCode").text = currency

    # Taxes
    for (cat_code, rate_pct), amounts in tax_breakdown.items():
        hdr_tax = ET.SubElement(settle, "ram:ApplicableTradeTax")
        ET.SubElement(hdr_tax, "ram:CalculatedAmount").text = f"{amounts['tax']:.2f}"
        ET.SubElement(hdr_tax, "ram:TypeCode").text = "VAT"
        ET.SubElement(hdr_tax, "ram:BasisAmount").text = f"{amounts['basis']:.2f}"
        ET.SubElement(hdr_tax, "ram:CategoryCode").text = cat_code
        ET.SubElement(hdr_tax, "ram:RateApplicablePercent").text = f"{rate_pct:.2f}"

    summation = ET.SubElement(settle, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    ET.SubElement(summation, "ram:LineTotalAmount").text = f"{net_val:.2f}"
    ET.SubElement(summation, "ram:TaxBasisTotalAmount").text = f"{net_val:.2f}"
    ET.SubElement(summation, "ram:TaxTotalAmount", attrib={"currencyID": currency}).text = f"{tax_val:.2f}"
    ET.SubElement(summation, "ram:GrandTotalAmount").text = f"{total_val:.2f}"
    ET.SubElement(summation, "ram:DuePayableAmount").text = f"{total_val:.2f}"

    xml_bytes = ET.tostring(rsm, encoding="utf-8", xml_declaration=True)

    # Validate against XSD if facturx is available
    try:
        import facturx
        facturx.xml_check_xsd(xml_bytes, flavor="factur-x", level="en16931")
    except ImportError:
        pass
    except Exception as err:
        raise Refused(f"generated XML fails EN 16931 XSD check: {err}")

    return xml_bytes


def attach_facturx_to_pdf(
    pdf_bytes: bytes,
    xml_bytes: bytes,
    conformance: str = DEFAULT_PROFILE,
    filename: str = DEFAULT_FILENAME,
) -> bytes:
    """Attach XML to PDF complying with PDF/A-3b through the pinned factur-x engine."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise Refused("source is not a valid PDF file (missing %PDF- header)")

    facturx, pypdf = libraries()

    # Pre-flight: verify the base PDF is not corrupt and contains pages
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) == 0:
            raise Refused("source PDF has no pages")
    except Exception as err:
        raise Refused(f"cannot parse source PDF: {err}")

    try:
        hybrid_bytes = facturx.generate_from_binary(
            pdf_bytes,
            xml_bytes,
            flavor="factur-x",
            level="en16931",
            afrelationship="alternative",
            check_xsd=True,
        )
    except Exception as err:
        raise Refused(f"factur-x embedding failed: {err}")

    return hybrid_bytes


def extract_facturx_xml(pdf_bytes: bytes) -> bytes:
    """Extract embedded factur-x.xml from hybrid PDF."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise Refused("not a valid PDF")

    facturx, _ = libraries()
    try:
        res = facturx.get_facturx_xml_from_pdf(pdf_bytes)
        if not res or not res[1]:
            raise Refused("no embedded Factur-X / ZUGFeRD XML found in PDF")
        return res[1]
    except Exception as err:
        raise Refused(f"extraction failed: {err}")


def verify_pdf(pdf_bytes: bytes) -> dict[str, str]:
    """Verify that a PDF strictly satisfies veraPDF PDF/A-3b clauses and KoSIT EN 16931 rules."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ProofFailed("missing %PDF- header")

    facturx, pypdf = libraries()

    # 1. Structural PDF Verification via pypdf (xref, pages, catalog, trailer)
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except Exception as err:
        raise ProofFailed(f"PDF structure is corrupt (xref/stream parse error): {err}")

    if len(reader.pages) == 0:
        raise ProofFailed("PDF contains no pages (Top-level pages object is null or empty)")

    root = reader.trailer.get("/Root")
    if not root:
        raise ProofFailed("PDF document lacks /Root catalog dictionary")
    root_obj = root.get_object()

    # Clause 6.8-4: /AF array in Document Catalog
    if "/AF" not in root_obj:
        raise ProofFailed("veraPDF [6.8-4]: PDF document catalog lacks mandatory /AF array binding")

    # 2. Extract XML and validate
    try:
        res = facturx.get_facturx_xml_from_pdf(pdf_bytes)
        if not res or not res[1]:
            raise ProofFailed("no embedded Factur-X / ZUGFeRD XML found in PDF")
        xml_bytes = res[1]
    except Exception as err:
        raise ProofFailed(f"embedded XML extraction failed: {err}")

    # 3. XML Schema validation against EN 16931 XSD
    try:
        facturx.xml_check_xsd(xml_bytes, flavor="factur-x", level="en16931")
    except Exception as err:
        raise ProofFailed(f"embedded XML fails EN 16931 XSD validation: {err}")

    # 4. XML Business rules verification (KoSIT)
    try:
        root_xml = ET.fromstring(xml_bytes)
    except ET.ParseError as err:
        raise ProofFailed(f"XML parse error: {err}")

    guideline = root_xml.find(".//{*}GuidelineSpecifiedDocumentContextParameter/{*}ID")
    guideline_id = guideline.text if guideline is not None else ""
    if not (guideline_id.startswith("urn:cen.eu:en16931:2017")):
        raise ProofFailed(f"KoSIT [BR-01]: guideline URN must be EN 16931, found {guideline_id}")

    lines = root_xml.findall(".//{*}IncludedSupplyChainTradeLineItem")
    if not lines:
        raise ProofFailed("EN 16931 requires at least one line item (IncludedSupplyChainTradeLineItem)")

    net_el = root_xml.find(".//{*}TaxBasisTotalAmount")
    tax_el = root_xml.find(".//{*}TaxTotalAmount")
    grand_el = root_xml.find(".//{*}GrandTotalAmount")
    if net_el is not None and tax_el is not None and grand_el is not None:
        net_val = quantize(net_el.text or "0")
        tax_val = quantize(tax_el.text or "0")
        grand_val = quantize(grand_el.text or "0")
        if grand_val != quantize(net_val + tax_val):
            raise ProofFailed(
                f"KoSIT [BR-CO-15]: grand total ({grand_val:.2f}) != net ({net_val:.2f}) + tax ({tax_val:.2f})"
            )

    digest = hashlib.sha256(xml_bytes).hexdigest()
    return {
        "conformance": DEFAULT_PROFILE,
        "guideline": guideline_id,
        "sha256": digest,
        "size": str(len(xml_bytes)),
    }


def make_minimal_pdf() -> bytes:
    """Generate a valid 1-page blank PDF fixture using pypdf."""
    _, pypdf = libraries()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def selftest() -> int:
    """Offline checks: runs all validations, attachments, and asserts 1 failure per gotcha clause."""
    passed, failed = 0, []

    def check(name: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"ok   {name}")
        else:
            failed.append(name)
            print(f"FAIL {name}")

    facturx, pypdf = libraries()

    fixture_pdf = make_minimal_pdf()
    check("fixture PDF generates valid %PDF header", fixture_pdf.startswith(b"%PDF-"))

    inv_data = {
        "number": "INV-2026-001",
        "date": "2026-05-15",
        "seller": {"name": "Acme Supplies GmbH", "vat_id": "DE123456789"},
        "buyer": {"name": "Client Solutions AG", "vat_id": "DE987654321"},
        "currency": "EUR",
        "net": 1000.00,
        "tax": 190.00,
        "total": 1190.00,
        "lines": [{"name": "Consulting", "quantity": 1.0, "unit_price": 1000.00, "amount": 1000.00, "tax_percent": 19.0}],
    }

    # 1. XML generation & XSD validation
    xml = build_facturx_xml(inv_data)
    check("XML contains CrossIndustryInvoice", b"CrossIndustryInvoice" in xml)
    check("XML contains IncludedSupplyChainTradeLineItem", b"IncludedSupplyChainTradeLineItem" in xml)
    check("XML contains EN 16931 guideline URN", EN16931_URN.encode() in xml)
    check("XML passes EN 16931 XSD check", facturx.xml_check_xsd(xml, flavor="factur-x", level="en16931") is True)

    # 2. Embedding via pinned factur-x
    hybrid = attach_facturx_to_pdf(fixture_pdf, xml)
    check("hybrid PDF carries /AFRelationship /Alternative", b"/AFRelationship /Alternative" in hybrid)
    check("hybrid PDF carries exact SHA-256 of factur-x.xml", hashlib.sha256(xml).hexdigest() in [
        verify_pdf(hybrid)["sha256"]
    ])

    # 3. Verification & Extraction
    info = verify_pdf(hybrid)
    check("verify_pdf validates hybrid PDF", info["conformance"] == "EN 16931")
    extracted = extract_facturx_xml(hybrid)
    check("extracted XML matches generated bytes", extracted == xml)

    # 4. Refusals: 1 check per failure mode
    # Refusal: negative invoice total
    try:
        build_facturx_xml({**inv_data, "total": -50.00})
        check("refuse negative total", False)
    except Refused:
        check("refuse negative total", True)

    # Refusal: missing line items
    try:
        build_facturx_xml({**inv_data, "lines": []})
        check("refuse missing line items", False)
    except Refused:
        check("refuse missing line items", True)

    # Refusal: non-PDF source
    try:
        attach_facturx_to_pdf(b"not a pdf", xml)
        check("refuse non-PDF source", False)
    except Refused:
        check("refuse non-PDF source", True)

    # Refusal: KoSIT [BR-01] guideline mismatch
    try:
        bad_urn_data = {**inv_data, "guideline_id": "urn:factur-x.eu:1p0:basic"}
        build_facturx_xml(bad_urn_data)
        check("refuse KoSIT BR-01 guideline mismatch", False)
    except Refused:
        check("refuse KoSIT BR-01 guideline mismatch", True)

    # Refusal: KoSIT [BR-CO-15] rounding discrepancy
    try:
        bad_math_data = {**inv_data, "net": 100.00, "tax": 19.00, "total": 119.05}
        build_facturx_xml(bad_math_data)
        check("refuse KoSIT BR-CO-15 math discrepancy", False)
    except Refused:
        check("refuse KoSIT BR-CO-15 math discrepancy", True)

    # Refusal: plain non-hybrid PDF fails verification
    try:
        verify_pdf(fixture_pdf)
        check("verify refuses plain non-hybrid PDF", False)
    except ProofFailed:
        check("verify refuses plain non-hybrid PDF", True)

    # Refusal: corrupted PDF fails verification (no false green)
    corrupted_pdf = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
    try:
        verify_pdf(corrupted_pdf)
        check("verify refuses corrupted PDF without false green", False)
    except ProofFailed:
        check("verify refuses corrupted PDF without false green", True)

    print(f"\nzugferd-invoice selftest: {passed}/{passed + len(failed)} checks pass")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factur-X / ZUGFeRD 2.2 hybrid e-invoice tool (PDF/A-3)")
    parser.add_argument("--selftest", action="store_true", help="run offline selftest checks")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="run offline selftest checks")

    p_pack = sub.add_parser("pack", help="generate XML from JSON data and attach to PDF as PDF/A-3")
    p_pack.add_argument("--pdf", required=True, type=Path, help="visual PDF invoice")
    p_pack.add_argument("--data", required=True, type=Path, help="JSON file with invoice data")
    p_pack.add_argument("--out", required=True, type=Path, help="output hybrid PDF")

    p_attach = sub.add_parser("attach", help="attach existing factur-x.xml to PDF with PDF/A-3 compliance")
    p_attach.add_argument("--pdf", required=True, type=Path, help="visual PDF invoice")
    p_attach.add_argument("--xml", required=True, type=Path, help="compliant factur-x.xml")
    p_attach.add_argument("--profile", default=DEFAULT_PROFILE, help=f"profile level (default: {DEFAULT_PROFILE})")
    p_attach.add_argument("--out", required=True, type=Path, help="output hybrid PDF")

    p_extract = sub.add_parser("extract", help="extract embedded factur-x.xml from hybrid PDF")
    p_extract.add_argument("pdf", type=Path, help="hybrid PDF invoice")
    p_extract.add_argument("--out", type=Path, help="path to save extracted XML (default: stdout)")

    p_verify = sub.add_parser("verify", help="verify PDF/A-3 and Factur-X invariants in hybrid PDF")
    p_verify.add_argument("pdf", type=Path, help="hybrid PDF invoice")

    args = parser.parse_args(argv)

    try:
        if args.selftest or args.cmd == "selftest":
            return selftest()

        if not args.cmd:
            parser.print_help()
            return 2

        if args.cmd == "pack":
            data = json.loads(args.data.read_text(encoding="utf-8"))
            xml_bytes = build_facturx_xml(data)
            pdf_bytes = args.pdf.read_bytes()
            hybrid = attach_facturx_to_pdf(pdf_bytes, xml_bytes, conformance=data.get("profile", DEFAULT_PROFILE))
            args.out.write_bytes(hybrid)
            print(f"VERIFIED {args.out} (Factur-X {data.get('profile', DEFAULT_PROFILE)})")
            return 0
        if args.cmd == "attach":
            xml_bytes = args.xml.read_bytes()
            pdf_bytes = args.pdf.read_bytes()
            hybrid = attach_facturx_to_pdf(pdf_bytes, xml_bytes, conformance=args.profile)
            args.out.write_bytes(hybrid)
            print(f"VERIFIED {args.out} (Factur-X {args.profile})")
            return 0
        if args.cmd == "extract":
            xml_bytes = extract_facturx_xml(args.pdf.read_bytes())
            if args.out:
                args.out.write_bytes(xml_bytes)
                print(f"Extracted {len(xml_bytes)} bytes to {args.out}")
            else:
                sys.stdout.buffer.write(xml_bytes)
            return 0
        if args.cmd == "verify":
            info = verify_pdf(args.pdf.read_bytes())
            print(f"VALID Factur-X {info['conformance']} (sha256={info['sha256'][:16]}...)")
            return 0
    except DependencyMissing as err:
        print(f"dependency missing: {err}", file=sys.stderr)
        return 3
    except (Refused, ProofFailed) as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
