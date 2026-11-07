"""A small but realistic .docx for tests: split runs, a multi-paragraph {if} block and a table row inside one."""
import io
import zipfile

CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def p(*runs):
    return "<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>' for t in runs) + "</w:p>"


def build(body_paras):
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body_paras) + '<w:sectPr/></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def sample():
    row = lambda a, b: f"<w:tr><w:tc>{p(a)}</w:tc><w:tc>{p(b)}</w:tc></w:tr>"  # noqa: E731
    table = f"<w:tbl>{row('Policy', '{Policy}')}{row('{if Fund > 5000}', '')}{row('Senior review', 'required')}{row('{endif}', '')}</w:tbl>"
    return build([
        p("Dear ", "{Hol", "der}", ","),                                  # a placeholder Word split into runs
        p("Your claim on {document_name} for ", "{Fund}", "."),
        p("{if Priority = High}"), p("This claim is marked HIGH priority."), p("{endif}"),
        p("Inline: {if Fund > 100}big{endif}{if Fund < 100}small{endif}."),
        table,
        p("Unknown: {No such field}."),
    ])
