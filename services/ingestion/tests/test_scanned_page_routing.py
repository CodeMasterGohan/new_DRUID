"""A real image-only PDF page is not a blank page."""
import pytest

from services.ingestion.native_worker import extract_pages_native
from services.ingestion.router import PageRoute, route_page


def write_image_and_blank_pdf(path):
    pytest.importorskip('pdftext')
    pytest.importorskip('pypdfium2')
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import (
        DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.set_data(b'\x20\x40\x60')
    image.update({NameObject('/Type'): NameObject('/XObject'),
        NameObject('/Subtype'): NameObject('/Image'),
        NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
        NameObject('/BitsPerComponent'): NumberObject(8),
        NameObject('/ColorSpace'): NameObject('/DeviceRGB')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'):
        DictionaryObject({NameObject('/Im0'): writer._add_object(image)})})
    content = DecodedStreamObject()
    content.set_data(b'q 400 0 0 500 50 50 cm /Im0 Do Q')
    page[NameObject('/Contents')] = writer._add_object(content)
    writer.add_blank_page(width=612, height=792)
    writer.write(path)


def test_image_only_page_routes_to_visual_but_blank_page_is_skipped(tmp_path):
    path = tmp_path / 'scan-and-blank.pdf'
    write_image_and_blank_pdf(path)
    pages = extract_pages_native(str(path), workers=1)
    assert len(pages) == 2
    assert route_page(pages[1].geometry).route == PageRoute.SKIP_BLANK
    assert route_page(pages[0].geometry).route == PageRoute.VISUAL


def test_selected_blank_page_keeps_its_original_page_index(tmp_path):
    path = tmp_path / 'scan-and-blank.pdf'
    write_image_and_blank_pdf(path)
    pages = extract_pages_native(str(path), page_range=[1], workers=1)
    assert len(pages) == 1 and pages[0].page_index == 1
    assert route_page(pages[0].geometry).route == PageRoute.SKIP_BLANK


def test_vector_only_page_routes_to_visual(tmp_path):
    pytest.importorskip('pdftext')
    pytest.importorskip('pypdfium2')
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    content = DecodedStreamObject()
    content.set_data(b'0.2 0.4 0.6 rg 50 50 400 500 re f')
    page[NameObject('/Contents')] = writer._add_object(content)
    path = tmp_path / 'vector.pdf'
    writer.write(path)
    pages = extract_pages_native(str(path), workers=1)
    assert route_page(pages[0].geometry).route == PageRoute.VISUAL
