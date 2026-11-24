---
name: zugferd-invoice
description: Use when creating, attaching, extracting, or validating a Factur-X / ZUGFeRD 2.2 hybrid electronic invoice (PDF/A-3b with embedded factur-x.xml conforming to EN 16931). Conforms to European e-invoicing standards for B2B and B2G. Generates compliant CrossIndustryInvoice XML from structured invoice data, embeds factur-x.xml into a visual invoice PDF with PDF/A-3 metadata, output intent, and catalog /AF relationships, extracts embedded XML from incoming hybrid invoices, and validates invoice totals, tax calculations, and veraPDF PDF/A-3b clause invariants.
license: Apache-2.0
compatibility: Requires Python 3.10+ with factur-x 7.9 and pypdf 7.0.0 in a virtual environment. Base PDF rendered with WeasyPrint 70.0+ (pdf_variant="pdf/a-3b"). No network, no account. Agent-agnostic.
metadata:
  version: "1.0.0"
---

# ZUGFeRD / Factur-X: Hybrid E-Invoices conforming to EN 16931

A standard visual PDF invoice cannot be read reliably by automated enterprise accounting systems,
while a pure XML invoice cannot be read by human recipients without specialized viewer software.
Factur-X and ZUGFeRD 2.2 standardize a single hybrid format: a human-readable PDF/A-3 document with
machine-readable CrossIndustryInvoice XML (`factur-x.xml`) embedded inside it.

Naive attempts by AI agents to attach XML invoices fail compliance checkers (veraPDF, KoSIT):

```
# veraPDF 1.30.4, -f 3b; pypdf 7.0.0, ReportLab 5.1.0, WeasyPrint 70.0, factur-x 7.9, Python 3.12
ReportLab + pypdf add_attachment
  7 failed: 6.8-1 Subtype, 6.8-2 F/UF, 6.8-3 AFRelationship, 6.8-4 not associated,
            6.6.2.1-1 Metadata, 6.2.4.3-4 DeviceGray, 6.2.11.4.1-1 fonts
WeasyPrint plain + factur-x
  2 failed: 6.1.3-1 trailer ID, 6.2.4.3-2 DeviceRGB without output intent
WeasyPrint pdf/a-3b + factur-x
  compliant, 146 rules passed
```

This skill uses the pinned reference `factur-x` engine and `pypdf` to parse object streams,
preserve the DeviceRGB ICC output intent, inject exact PDF/A-3b catalog `/AF` bindings, generate
EN 16931 compliant line items and tax breakdown, and verify structural and mathematical invariants
without false greens.

## Modes

- **`pack`**: Generates a valid Factur-X / ZUGFeRD 2.2 XML file (`factur-x.xml`) conforming to EN 16931
  with mandatory line items, embeds it into a PDF/A-3 base invoice, and verifies the result.
- **`attach`**: Embeds existing `factur-x.xml` into a base PDF/A-3 invoice via pinned `factur-x`
  with `/AFRelationship /Alternative`.
- **`extract`**: Extracts the embedded `factur-x.xml` from an incoming hybrid invoice PDF to disk.
- **`verify`**: Inspects a hybrid invoice, verifying that the PDF structure and pages parse cleanly,
  the embedded XML passes official EN 16931 XSD validation, and business rules (`[BR-01]`, `[BR-09]`,
  `[BR-11]`, `[BR-CO-10]`, `[BR-CO-14]`, `[BR-CO-15]`) hold.
- **`selftest`**: Runs the offline selftest suite validating end-to-end invoice generation, attachment,
  extraction, round-trip equality, and exact clause refusals.

## Install

In a virtual environment with Python 3.10+:

```bash
python3 -m venv .venv-invoice
.venv-invoice/bin/python -m pip install factur-x==7.9 pypdf==7.0.0
.venv-invoice/bin/python scripts/invoice.py --selftest
```

The selftest runs offline and must end with `zugferd-invoice selftest: N/N checks pass`.

| Exit code | Meaning |
| --- | --- |
| 0 | Verified, generated, or extracted successfully |
| 1 | Refused: invalid mathematical totals, missing required fields, or non-compliant XML |
| 2 | Usage error, missing argument, or file not found |
| 3 | Dependency missing (`factur-x` or `pypdf` not installed in environment) |
| 4 | Proof failed: PDF structure corrupt, XSD validation error, or invariant violation |

## Usage

### 1. Generating Factur-X XML and Packing (`pack`)

Pass a JSON file containing the invoice header, seller, buyer, line items, and tax rates:

```bash
python3 scripts/invoice.py pack \
    --pdf print_invoice.pdf \
    --data invoice.json \
    --out hybrid_invoice.pdf
```

Expected JSON format:

```json
{
  "number": "INV-2026-0042",
  "date": "2026-11-07",
  "currency": "EUR",
  "seller": {
    "name": "Acme Solutions GmbH",
    "vat_id": "DE123456789",
    "country": "DE",
    "city": "Munich",
    "postal_code": "80331",
    "street": "Maximilianstr. 10"
  },
  "buyer": {
    "name": "Global Logistics S.A.",
    "vat_id": "FR98765432101",
    "country": "FR",
    "city": "Paris",
    "postal_code": "75008",
    "street": "Boulevard Haussmann 25"
  },
  "lines": [
    {
      "name": "Cloud Engineering Consulting",
      "quantity": 1.0,
      "unit_price": 1000.00,
      "amount": 1000.00,
      "unit_code": "C62",
      "tax_percent": 19.0
    }
  ],
  "net": 1000.00,
  "tax": 190.00,
  "total": 1190.00
}
```

### 2. Attaching XML to an Existing Invoice PDF (`attach`)

When you already have a visual invoice PDF produced by WeasyPrint (`pdf_variant="pdf/a-3b"`):

```bash
python3 scripts/invoice.py attach \
    --pdf print_invoice.pdf \
    --xml factur-x.xml \
    --out hybrid_invoice.pdf
```

The pinned engine embeds `factur-x.xml` while preserving the ICC output intent and adding:
1. `Clause 6.8-1`: Embedded stream with `/Subtype /text#2Fxml`.
2. `Clause 6.8-2` & `6.8-3`: File specification dictionary with `/F`, `/UF` and `/AFRelationship /Alternative`.
3. `Clause 6.8-4`: Document Catalog `/AF` array pointing to the file specification.
4. `Clause 6.6.2.1-1`: XMP metadata stream declaring PDF/A-3b and Factur-X extension schema.

### 3. Extracting XML from an Incoming Hybrid Invoice (`extract`)

To inspect or process an invoice received from a supplier:

```bash
python3 scripts/invoice.py extract received_invoice.pdf --out supplier_factur-x.xml
```

### 4. Verifying an Invoice (`verify`)

Check both the visual wrapper and the embedded XML without false greens:

```bash
python3 scripts/invoice.py verify hybrid_invoice.pdf
```

Output:
```
VALID Factur-X EN 16931 (sha256=a1b2c3d4e5f60718...)
```

Invariants checked:
- `pypdf` structural parse: valid xref, non-empty pages list, and `/Root` catalog.
- `veraPDF [6.8-4]`: Document catalog contains `/AF` array.
- `facturx` extraction: extracts `factur-x.xml` from stream without failure.
- `facturx.xml_check_xsd`: validates embedded XML against official EN 16931 XSD schemas.
- `KoSIT [BR-01]`: Guideline URN is `urn:cen.eu:en16931:2017`.
- `CEN [BR-08] / [BR-09]`: Seller postal address (`BG-5`) contains a Seller country code (`BT-40`).
- `CEN [BR-10] / [BR-11]`: Buyer postal address (`BG-8`) contains a Buyer country code (`BT-55`).
- `CEN [BR-CO-10]`: Sum of invoice line net amounts (`BT-106`) equals Σ invoice line net amounts (`BT-131`).
- `CEN [BR-CO-14]`: Invoice total VAT amount (`BT-110`) equals Σ VAT category tax amounts (`BT-117`).
- `KoSIT [BR-CO-15]`: Grand total equals net plus tax (`BT-112 = BT-109 + BT-110`).
