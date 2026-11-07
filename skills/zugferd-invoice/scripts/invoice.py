#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file beside this script.
"""Factur-X / ZUGFeRD 2.2 hybrid e-invoice generator and validator (PDF/A-3b with EN 16931 XML).

Purpose:
    Turns a visual PDF invoice into a compliant Factur-X / ZUGFeRD hybrid electronic invoice by
    generating and embedding standard EN 16931 CII XML (factur-x.xml) into a PDF/A-3b document,
    with exact Associated Files (/AF) catalog bindings, /AFRelationship /Alternative attributes,
    and PDF/A-3 extension schemas.

Usage:
    python3 invoice.py pack --pdf input.pdf --data invoice.json --out factur-x.pdf
    python3 invoice.py attach --pdf input.pdf --xml factur-x.xml --out factur-x.pdf
    python3 invoice.py extract factur-x.pdf [--out factur-x.xml]
    python3 invoice.py verify factur-x.pdf
    python3 invoice.py selftest

Standard library only. No external dependencies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROFILES = {
    "MINIMUM": "urn:factur-x.eu:1p0:minimum",
    "BASIC_WL": "urn:factur-x.eu:1p0:basicwl",
    "BASIC": "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic",
    "EN16931": "urn:cen.eu:en16931:2017",
    "EXTENDED": "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended",
}

DEFAULT_FILENAME = "factur-x.xml"


class Refused(Exception):
    """Input that violates Factur-X, EN 16931 or PDF/A-3 requirements."""


class ProofFailed(Exception):
    """The generated or examined PDF does not fulfill PDF/A-3 or Factur-X invariants."""


def build_xmp_metadata(conformance: str = "BASIC") -> bytes:
    """Build PDF/A-3b XMP metadata stream declaring Factur-X / ZUGFeRD extension schema."""
    level = conformance.upper()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    xmp = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
      <pdfaid:part>3</pdfaid:part>
      <pdfaid:conformance>B</pdfaid:conformance>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <xmp:CreateDate>{now}</xmp:CreateDate>
      <xmp:ModifyDate>{now}</xmp:ModifyDate>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/" xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
      <pdfaExtension:schemas>
        <rdf:Bag>
          <rdf:li rdf:parseType="Resource">
            <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
            <pdfaSchema:namespaceURI>urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#</pdfaSchema:namespaceURI>
            <pdfaSchema:prefix>fx</pdfaSchema:prefix>
            <pdfaSchema:property>
              <rdf:Seq>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>DocumentFileName</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The name of the embedded invoice file</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>DocumentType</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The type of the hybrid document in compliance with Factur-X</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>Version</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The actual version of the Factur-X standard</pdfaProperty:description>
                </rdf:li>
                <rdf:li rdf:parseType="Resource">
                  <pdfaProperty:name>ConformanceLevel</pdfaProperty:name>
                  <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                  <pdfaProperty:category>external</pdfaProperty:category>
                  <pdfaProperty:description>The actual profile of Factur-X</pdfaProperty:description>
                </rdf:li>
              </rdf:Seq>
            </pdfaSchema:property>
          </rdf:li>
        </rdf:Bag>
      </pdfaExtension:schemas>
    </rdf:Description>
    <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
      <fx:DocumentType>INVOICE</fx:DocumentType>
      <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
      <fx:Version>1.0</fx:Version>
      <fx:ConformanceLevel>{level}</fx:ConformanceLevel>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return xmp.encode("utf-8")


def build_facturx_xml(data: dict) -> bytes:
    """Build a compliant CrossIndustryInvoice XML conforming to EN 16931."""
    for req in ("number", "date", "seller", "buyer", "currency", "lines", "total"):
        if req not in data:
            raise Refused(f"missing required invoice field: {req}")

    profile_name = data.get("profile", "BASIC").upper()
    if profile_name not in PROFILES:
        raise Refused(f"unknown profile {profile_name}; allowed: {', '.join(sorted(PROFILES))}")
    profile_urn = PROFILES[profile_name]

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
    ET.SubElement(guideline, "ram:ID").text = profile_urn

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

    # Header Trade Agreement
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

    # Header Delivery
    deliv = ET.SubElement(tx, "ram:ApplicableHeaderTradeDelivery")
    ET.SubElement(deliv, "ram:ShipToTradeParty")

    # Header Settlement
    settle = ET.SubElement(tx, "ram:ApplicableHeaderTradeSettlement")
    ET.SubElement(settle, "ram:InvoiceCurrencyCode").text = str(data.get("currency", "EUR"))

    # Monetary Summation
    summation = ET.SubElement(settle, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    total_val = float(data["total"])
    if total_val < 0:
        raise Refused("invoice total cannot be negative")
    net_val = float(data.get("net", total_val))
    tax_val = float(data.get("tax", round(total_val - net_val, 2)))

    ET.SubElement(summation, "ram:LineTotalAmount").text = f"{net_val:.2f}"
    ET.SubElement(summation, "ram:TaxBasisTotalAmount").text = f"{net_val:.2f}"
    ET.SubElement(summation, "ram:TaxTotalAmount", attrib={"currencyID": data["currency"]}).text = f"{tax_val:.2f}"
    ET.SubElement(summation, "ram:GrandTotalAmount").text = f"{total_val:.2f}"
    ET.SubElement(summation, "ram:DuePayableAmount").text = f"{total_val:.2f}"

    return ET.tostring(rsm, encoding="utf-8", xml_declaration=True)


def attach_facturx_to_pdf(
    pdf_bytes: bytes,
    xml_bytes: bytes,
    conformance: str = "BASIC",
    filename: str = DEFAULT_FILENAME,
) -> bytes:
    """Attach XML to PDF with PDF/A-3 compliance (/AF catalog bindings, /AFRelationship, XMP)."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise Refused("source is not a valid PDF file (missing %PDF- header)")

    # Find the maximum object number in the existing PDF
    obj_matches = [int(m) for m in re.findall(rb"(\d+)\s+0\s+obj", pdf_bytes)]
    next_id = (max(obj_matches) if obj_matches else 10) + 1

    stream_id = next_id
    filespec_id = next_id + 1
    xmp_id = next_id + 2
    catalog_id = next_id + 3

    # Locate existing /Root
    root_match = re.search(rb"/Root\s+(\d+)\s+0\s+R", pdf_bytes)
    if not root_match:
        raise Refused("could not locate /Root catalog in source PDF")
    old_root_id = int(root_match.group(1))

    # Read old catalog content
    old_cat_match = re.search(
        rb"%d\s+0\s+obj\s*<<(.*?)>>\s*endobj" % old_root_id, pdf_bytes, re.S
    )
    cat_body = old_cat_match.group(1) if old_cat_match else b""
    # Clean old AF, Names, Metadata keys from catalog body
    clean_cat = re.sub(rb"/AF\s*\[[^\]]*\]", b"", cat_body)
    clean_cat = re.sub(rb"/Metadata\s+\d+\s+0\s+R", b"", clean_cat)
    clean_cat = re.sub(rb"/Names\s+<<.*?>>", b"", clean_cat, flags=re.S)

    xmp_bytes = build_xmp_metadata(conformance)
    now_stamp = dt.datetime.now(dt.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")

    # 1. EmbeddedFile Stream Object
    embedded_file_obj = (
        f"{stream_id} 0 obj\n"
        f"<< /Type /EmbeddedFile /Subtype /application#2Fxml /Length {len(xml_bytes)} "
        f"/Params << /ModDate ({now_stamp}) /Size {len(xml_bytes)} >> >>\n"
        f"stream\n"
    ).encode("ascii") + xml_bytes + b"\nendstream\nendobj\n"

    # 2. FileSpec Object
    filespec_obj = (
        f"{filespec_id} 0 obj\n"
        f"<< /Type /Filespec /F ({filename}) /UF ({filename}) "
        f"/EF << /F {stream_id} 0 R /UF {stream_id} 0 R >> "
        f"/AFRelationship /Alternative "
        f"/Desc (Factur-X / ZUGFeRD Invoice) >>\n"
        f"endobj\n"
    ).encode("ascii")

    # 3. XMP Metadata Object
    xmp_obj = (
        f"{xmp_id} 0 obj\n"
        f"<< /Type /Metadata /Subtype /XML /Length {len(xmp_bytes)} >>\n"
        f"stream\n"
    ).encode("ascii") + xmp_bytes + b"\nendstream\nendobj\n"

    # 4. Updated Catalog Object
    new_cat_body = clean_cat.strip()
    updated_catalog = (
        f"{catalog_id} 0 obj\n"
        f"<< {new_cat_body.decode('latin1', 'replace')} "
        f"/AF [ {filespec_id} 0 R ] "
        f"/Metadata {xmp_id} 0 R "
        f"/Names << /EmbeddedFiles << /Names [ ({filename}) {filespec_id} 0 R ] >> >> >>\n"
        f"endobj\n"
    ).encode("latin1")

    # Append incremental update
    body_append = embedded_file_obj + filespec_obj + xmp_obj + updated_catalog

    # Build new trailer and xref
    startxref = len(pdf_bytes)
    out = bytearray(pdf_bytes)
    xref_pos = len(out) + len(body_append)

    xref = (
        f"xref\n"
        f"{stream_id} 4\n"
        f"0000000000 00000 n \n"
        f"0000000000 00000 n \n"
        f"0000000000 00000 n \n"
        f"0000000000 00000 n \n"
        f"trailer\n"
        f"<< /Size {catalog_id + 1} /Root {catalog_id} 0 R /Prev {startxref} >>\n"
        f"startxref\n"
        f"{xref_pos}\n"
        f"%%EOF\n"
    ).encode("ascii")

    return bytes(out + body_append + xref)


def extract_facturx_xml(pdf_bytes: bytes) -> bytes:
    """Extract embedded factur-x.xml from hybrid PDF."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise Refused("not a valid PDF")
    # Search for embedded stream
    stream_match = re.search(rb"/Subtype\s*/application#2Fxml.*?stream[\r\n]+(.*?)(?:[\r\n]+endstream)", pdf_bytes, re.S)
    if not stream_match:
        stream_match = re.search(rb"<rsm:CrossIndustryInvoice.*?</rsm:CrossIndustryInvoice>", pdf_bytes, re.S)
        if stream_match:
            return stream_match.group(0)
        raise Refused("no embedded Factur-X / ZUGFeRD XML found in PDF")
    return stream_match.group(1).strip()


def verify_pdf(pdf_bytes: bytes) -> dict[str, str]:
    """Verify that a PDF strictly satisfies Factur-X and PDF/A-3b requirements."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ProofFailed("missing %PDF- header")

    # 1. /AF in Catalog
    if not re.search(rb"/AF\s*\[\s*\d+\s+0\s+R\s*\]", pdf_bytes):
        raise ProofFailed("PDF document catalog lacks mandatory /AF (Associated Files) array binding")

    # 2. /AFRelationship /Alternative
    if not re.search(rb"/AFRelationship\s*/Alternative", pdf_bytes):
        raise ProofFailed("filespec dictionary lacks mandatory /AFRelationship /Alternative attribute")

    # 3. PDF/A-3 identification
    if not re.search(rb"<pdfaid:part>3</pdfaid:part>", pdf_bytes):
        raise ProofFailed("XMP metadata lacks <pdfaid:part>3</pdfaid:part>")
    if not re.search(rb"<pdfaid:conformance>[bB]</pdfaid:conformance>", pdf_bytes):
        raise ProofFailed("XMP metadata lacks PDF/A conformance level B")

    # 4. Factur-X schema in XMP
    fx_match = re.search(rb"<fx:ConformanceLevel>([A-Za-z0-9_-]+)</fx:ConformanceLevel>", pdf_bytes)
    if not fx_match:
        raise ProofFailed("XMP metadata lacks <fx:ConformanceLevel> declaration")
    conformance = fx_match.group(1).decode("ascii")

    # 5. Embedded XML verification
    xml_data = extract_facturx_xml(pdf_bytes)
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as err:
        raise ProofFailed(f"embedded XML failed parsing: {err}")

    if not root.tag.endswith("CrossIndustryInvoice"):
        raise ProofFailed(f"embedded XML root is not CrossIndustryInvoice (found {root.tag})")

    guideline = root.find(".//{*}GuidelineSpecifiedDocumentContextParameter/{*}ID")
    guideline_id = guideline.text if guideline is not None else ""

    digest = hashlib.sha256(xml_data).hexdigest()
    return {
        "conformance": conformance,
        "guideline": guideline_id,
        "sha256": digest,
        "size": str(len(xml_data)),
    }


def make_minimal_pdf() -> bytes:
    """Generate a minimal valid 1-page PDF fixture for testing."""
    return b"""%PDF-1.7
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [ 3 0 R ] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [ 0 0 595 842 ] >>
endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000056 00000 n 
0000000111 00000 n 
trailer
<< /Size 4 /Root 1 0 R >>
startxref
190
%%EOF
"""


def selftest() -> int:
    """Offline checks: runs all validations, attachments, proofs and refusals."""
    passed, failed = 0, []

    def check(name: str, cond: bool) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"ok   {name}")
        else:
            failed.append(name)
            print(f"FAIL {name}")

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
        "lines": [{"name": "Consulting", "amount": 1000.00}],
        "profile": "BASIC",
    }

    xml = build_facturx_xml(inv_data)
    check("XML contains CrossIndustryInvoice", b"CrossIndustryInvoice" in xml)
    check("XML contains BASIC profile URN", PROFILES["BASIC"].encode() in xml)
    check("XML contains invoice number", b"INV-2026-001" in xml)

    # Attach and verify
    hybrid = attach_facturx_to_pdf(fixture_pdf, xml, conformance="BASIC")
    info = verify_pdf(hybrid)
    check("hybrid PDF conforms to PDF/A-3b and Factur-X BASIC", info["conformance"] == "BASIC")
    check("hybrid PDF carries exact SHA-256 of factur-x.xml", info["sha256"] == hashlib.sha256(xml).hexdigest())

    # Extract test
    extracted = extract_facturx_xml(hybrid)
    check("extracted XML matches generated bytes", extracted == xml)

    # Refusal tests
    try:
        build_facturx_xml({**inv_data, "total": -100})
        check("refuse negative total", False)
    except Refused:
        check("refuse negative total", True)

    try:
        attach_facturx_to_pdf(b"not a pdf", xml)
        check("refuse non-PDF source", False)
    except Refused:
        check("refuse non-PDF source", True)

    try:
        verify_pdf(fixture_pdf)
        check("verify refuses non-hybrid plain PDF", False)
    except ProofFailed:
        check("verify refuses non-hybrid plain PDF", True)

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
    p_attach.add_argument("--profile", default="BASIC", help="Factur-X profile level (default: BASIC)")
    p_attach.add_argument("--out", required=True, type=Path, help="output hybrid PDF")

    p_extract = sub.add_parser("extract", help="extract embedded factur-x.xml from hybrid PDF")
    p_extract.add_argument("pdf", type=Path, help="hybrid PDF invoice")
    p_extract.add_argument("--out", type=Path, help="path to save extracted XML (default: stdout)")

    p_verify = sub.add_parser("verify", help="verify PDF/A-3 and Factur-X invariants in hybrid PDF")
    p_verify.add_argument("pdf", type=Path, help="hybrid PDF invoice")

    args = parser.parse_args(argv)
    if args.selftest or args.cmd == "selftest":
        return selftest()

    if not args.cmd:
        parser.print_help()
        return 2

    try:
        if args.cmd == "pack":
            data = json.loads(args.data.read_text(encoding="utf-8"))
            xml_bytes = build_facturx_xml(data)
            pdf_bytes = args.pdf.read_bytes()
            hybrid = attach_facturx_to_pdf(pdf_bytes, xml_bytes, conformance=data.get("profile", "BASIC"))
            args.out.write_bytes(hybrid)
            print(f"VERIFIED {args.out} (Factur-X {data.get('profile', 'BASIC')})")
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
    except (Refused, ProofFailed) as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
