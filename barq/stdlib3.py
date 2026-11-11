"""Standard library, batch 3 (v0.6.0): Xml, Mail, Queue, Cli, Test.
(The web half of batch 3 — Route, Session, Cookie — is in web.py.)
Only the Python standard library is used.
"""

import base64
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import xml.parsers.expat as expat

from .builtins import need
from .runtime import (BarqClass, BarqObject, BarqSet, BarqThrow, BoundMethod, NativeFunction, to_display, type_name,
                      values_equal)
from .stdlib import arg, module
from .stdlib2 import EMAIL_RE, config_lookup, native_class, new_native
from .web import is_callable, new_test_client

STDLIB3_EXCEPTIONS = [
    ("XmlException", "ValueException"),
    ("MailException", "Exception"),
    ("QueueException", "Exception"),
    ("AssertionException", "Exception"),
]


def show(i, v):
    """A value as it appears in messages: strings quoted."""
    return to_display(v, i, True)


# ===========================================================================
# Xml
# ===========================================================================

XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


class XmlForbidden(Exception):
    pass


def new_xml_node(i, name, attrs=None, text=""):
    return new_native(i, "XmlNode", None, name=name, attrs=dict(attrs or {}), text=text, children=[])


def xml_parse_text(i, text, n, fname="Xml.parse"):
    stack, parts, root = [], [], []
    p = expat.ParserCreate()
    p.buffer_text = True

    def start(name, attrs):
        node = new_xml_node(i, name, attrs)
        if stack:
            stack[-1].fields["children"].append(node)
        else:
            root.append(node)
        stack.append(node)
        parts.append([])

    def end(name):
        node = stack.pop()
        node.fields["text"] = "".join(parts.pop()).strip()

    def chars(data):
        if parts:
            parts[-1].append(data)

    def no_entities(*a):
        raise XmlForbidden()

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    p.EntityDeclHandler = no_entities
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        p.Parse(text, True)
    except XmlForbidden:
        i.throw("XmlException", f"{fname}(): <!ENTITY> declarations are not allowed (they can be used for attacks)", n)
    except expat.ExpatError as e:
        i.throw("XmlException", f"{fname}(): {expat.errors.messages[e.code]} at line {e.lineno}, column {e.offset + 1}", n)
    return root[0]


def xml_escape(s, attr=False):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if attr:
        s = s.replace('"', "&quot;").replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")
    return s


def xml_scalar(i, v, n, what):
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return to_display(v, i)
    i.throw("TypeException", f"XML {what} must be a string or number, got {type_name(v)}", n)


def need_node(i, v, n, fname, pos=1):
    if not (isinstance(v, BarqObject) and v.cls.name == "XmlNode"):
        i.throw("TypeException", f"{fname}() argument {pos} must be XmlNode, got {type_name(v)}", n)
    return v


def xml_serialize(i, node, pretty, n, depth=0, seen=None):
    seen = seen or set()
    if id(node) in seen:
        i.throw("XmlException", "an XmlNode cannot contain itself", n)
    seen = seen | {id(node)}
    f = node.fields
    name = f.get("name")
    if not isinstance(name, str) or not XML_NAME.match(name):
        i.throw("XmlException", f"invalid XML element name {show(i, name)}", n)
    out = "<" + name
    attrs = f.get("attrs") or {}
    if not isinstance(attrs, dict):
        i.throw("TypeException", f"XmlNode.attrs must be a dict, got {type_name(attrs)}", n)
    for k, v in attrs.items():
        if not isinstance(k, str) or not XML_NAME.match(k):
            i.throw("XmlException", f"invalid XML attribute name {show(i, k)}", n)
        out += f' {k}="{xml_escape(xml_scalar(i, v, n, "attribute values"), True)}"'
    text = xml_escape(xml_scalar(i, f.get("text"), n, "text"))
    children = f.get("children") or []
    if not isinstance(children, list):
        i.throw("TypeException", f"XmlNode.children must be a list, got {type_name(children)}", n)
    if not children:
        return out + (f">{text}</{name}>" if text else "/>")
    pad = "\n" + "    " * (depth + 1) if pretty else ""
    inner = (pad + text if text else "")
    for c in children:
        inner += pad + xml_serialize(i, need_node(i, c, n, "XmlNode.children"), pretty, n, depth + 1, seen)
    end = "\n" + "    " * depth if pretty else ""
    return out + ">" + inner + end + f"</{name}>"


def node_to_value_fixed(node):
    """Like node_to_value, but repeated children always become a list."""
    f = node.fields
    attrs, children, text = f["attrs"], f["children"], f["text"]
    if not attrs and not children:
        return text
    d = {}
    for k, v in attrs.items():
        d["@" + k] = v
    counts = {}
    for c in children:
        counts[c.fields["name"]] = counts.get(c.fields["name"], 0) + 1
    for c in children:
        key, v = c.fields["name"], node_to_value_fixed(c)
        if counts[key] > 1:
            d.setdefault(key, []).append(v)
        else:
            d[key] = v
    if text:
        d["#text"] = text
    return d


def value_to_node(i, name, value, n):
    if not XML_NAME.match(name):
        i.throw("XmlException", f"Xml.fromDict(): invalid element name {show(i, name)}", n)
    node = new_xml_node(i, name)
    f = node.fields
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                i.throw("XmlException", f"Xml.fromDict(): keys must be strings, got {type_name(k)}", n)
            if k.startswith("@"):
                f["attrs"][k[1:]] = xml_scalar(i, v, n, "attribute values")
            elif k == "#text":
                f["text"] = xml_scalar(i, v, n, "text")
            elif isinstance(v, list):
                f["children"].extend(value_to_node(i, k, x, n) for x in v)
            else:
                f["children"].append(value_to_node(i, k, v, n))
    elif isinstance(value, list):
        i.throw("XmlException", f"Xml.fromDict(): a list must be the value of a key (the element name to repeat), at <{name}>", n)
    else:
        f["text"] = xml_scalar(i, value, n, "text")
    return node


def xml_from_dict(i, a, n):
    root = need(i, a[0], ("string",), "Xml.fromDict", 1, n)
    pretty = arg(i, a, 2, ("bool",), "Xml.fromDict", n, False)
    return xml_serialize(i, value_to_node(i, root, a[1], n), pretty, n)


def xml_valid(i, a, n):
    try:
        xml_parse_text(i, need(i, a[0], ("string",), "Xml.valid", 1, n), n)
        return True
    except BarqThrow as t:
        if t.obj.cls.name == "XmlException":
            return False
        raise


def xml_element(i, a, n):
    name = need(i, a[0], ("string",), "Xml.element", 1, n)
    if not XML_NAME.match(name):
        i.throw("XmlException", f"Xml.element(): invalid element name {show(i, name)}", n)
    attrs = arg(i, a, 1, ("dict",), "Xml.element", n, {})
    text = xml_scalar(i, a[2] if len(a) > 2 else None, n, "text")
    return new_xml_node(i, name, {k: xml_scalar(i, v, n, "attribute values") for k, v in attrs.items()}, text)


XML = {
    "parse": (lambda i, a, n: xml_parse_text(i, need(i, a[0], ("string",), "Xml.parse", 1, n), n), 1, 1),
    "valid": (xml_valid, 1, 1),
    "toDict": (lambda i, a, n: (lambda r: {r.fields["name"]: node_to_value_fixed(r)})(
        xml_parse_text(i, need(i, a[0], ("string",), "Xml.toDict", 1, n), n, "Xml.toDict")), 1, 1),
    "fromDict": (xml_from_dict, 2, 3),
    "element": (xml_element, 1, 3),
    "encode": (lambda i, a, n: xml_serialize(i, need_node(i, a[0], n, "Xml.encode"),
                                             arg(i, a, 1, ("bool",), "Xml.encode", n, False), n), 1, 2),
    "escape": (lambda i, a, n: xml_escape(need(i, a[0], ("string",), "Xml.escape", 1, n), True), 1, 1),
}


def xml_select(node, path):
    current, desc = [node], False
    for seg in path.split("/"):
        if seg == "":
            desc = True
            continue
        nxt = []
        for c in current:
            pool = xml_descendants(c) if desc else c.fields["children"]
            nxt.extend(x for x in pool if isinstance(x, BarqObject) and (seg == "*" or x.fields["name"] == seg))
        current, desc = nxt, False
    return current


def xml_descendants(node):
    out = []
    for c in node.fields["children"]:
        if isinstance(c, BarqObject):
            out.append(c)
            out.extend(xml_descendants(c))
    return out


def xn_find(i, a, n, this):
    found = xml_select(this, need(i, a[0], ("string",), "XmlNode.find", 1, n))
    return found[0] if found else None


def xn_text_of(i, a, n, this):
    found = xml_select(this, need(i, a[0], ("string",), "XmlNode.textOf", 1, n))
    return found[0].fields["text"] if found else (a[1] if len(a) > 1 else None)


def xn_add(i, a, n, this):
    child = need_node(i, a[0], n, "XmlNode.add")
    this.fields["children"].append(child)
    return child


def xn_set_attr(i, a, n, this):
    name = need(i, a[0], ("string",), "XmlNode.setAttr", 1, n)
    if not XML_NAME.match(name):
        i.throw("XmlException", f"XmlNode.setAttr(): invalid attribute name {show(i, name)}", n)
    this.fields["attrs"][name] = xml_scalar(i, a[1], n, "attribute values")
    return this


XML_NODE_METHODS = {
    "attr": (lambda i, a, n, this: this.fields["attrs"].get(need(i, a[0], ("string",), "XmlNode.attr", 1, n),
                                                             a[1] if len(a) > 1 else None), 1, 2),
    "setAttr": (xn_set_attr, 2, 2),
    "find": (xn_find, 1, 1),
    "findAll": (lambda i, a, n, this: xml_select(this, need(i, a[0], ("string",), "XmlNode.findAll", 1, n)), 1, 1),
    "textOf": (xn_text_of, 1, 2),
    "add": (xn_add, 1, 1),
    "toDict": (lambda i, a, n, this: node_to_value_fixed(this), 0, 0),
    "toString": (lambda i, a, n, this: xml_serialize(i, this, arg(i, a, 0, ("bool",), "XmlNode.toString", n, False), n), 0, 1),
    "__toString": (lambda i, a, n, this: xml_serialize(i, this, False, n), 0, 0),
}


# ===========================================================================
# Mail
# ===========================================================================

MAIL_DEFAULTS = {"driver": "log", "host": "localhost", "port": 587, "username": None, "password": None,
                 "encryption": "tls", "timeout": 30, "from": None, "fromName": None, "path": "storage/mail"}
MAIL_ENV = {"driver": ("MAIL_MAILER", "MAIL_DRIVER"), "host": ("MAIL_HOST",), "port": ("MAIL_PORT",),
            "username": ("MAIL_USERNAME",), "password": ("MAIL_PASSWORD",), "encryption": ("MAIL_ENCRYPTION",),
            "from": ("MAIL_FROM_ADDRESS",), "fromName": ("MAIL_FROM_NAME",)}
MAIL_DRIVERS = ("smtp", "log", "file", "array")


class MailState:
    def __init__(self, settings):
        self.settings = settings
        self.sent = []


def mail_state(i):
    st = getattr(i, "_mail", None)
    if st is None:
        cfg = dict(MAIL_DEFAULTS)
        for key, names in MAIL_ENV.items():
            for env in names:
                if os.environ.get(env):
                    cfg[key] = os.environ[env]
                    break
        conf = config_lookup(i, "mail")
        if isinstance(conf, dict):
            cfg.update({k: v for k, v in conf.items() if k in MAIL_DEFAULTS})
            if isinstance(conf.get("from"), dict):
                cfg["from"] = conf["from"].get("address")
                cfg["fromName"] = conf["from"].get("name", cfg["fromName"])
        if isinstance(cfg["port"], str) and cfg["port"].isdigit():
            cfg["port"] = int(cfg["port"])
        st = i._mail = MailState(cfg)
    return st


def mail_config(i, a, n):
    opts = need(i, a[0], ("dict",), "Mail.config", 1, n)
    st = mail_state(i)
    for k, v in opts.items():
        if k not in MAIL_DEFAULTS:
            i.throw("ValueException", f'Mail.config(): unknown option "{k}" (use {", ".join(MAIL_DEFAULTS)})', n)
        st.settings[k] = v
    if st.settings["driver"] not in MAIL_DRIVERS:
        i.throw("ValueException", f'Mail.config(): driver must be one of {", ".join(MAIL_DRIVERS)}', n)
    return None


def check_address(i, addr, n, fname):
    import email.utils
    if not isinstance(addr, str):
        i.throw("TypeException", f"{fname}(): an email address must be a string, got {type_name(addr)}", n)
    if "\r" in addr or "\n" in addr:
        i.throw("MailException", f"{fname}(): email address contains a line break", n)
    name, email_addr = email.utils.parseaddr(addr)
    if not EMAIL_RE.match(email_addr or ""):
        i.throw("MailException", f'{fname}(): invalid email address "{addr}"', n)
    return addr


def addresses(i, v, n, fname):
    items = v if isinstance(v, list) else [v]
    return [check_address(i, x, n, fname) for x in items]


class MessageState:
    def __init__(self):
        self.to, self.cc, self.bcc, self.reply_to = [], [], [], []
        self.sender = None
        self.subject = ""
        self.text = None
        self.html = None
        self.headers = {}
        self.attachments = []   # (name, mime, bytes)


def msg_list(field, fname):
    def fn(i, a, n, this):
        getattr(this.native, field).extend(addresses(i, a[0], n, fname))
        return this
    return fn


def msg_from(i, a, n, this):
    import email.utils
    addr = check_address(i, a[0], n, "Message.from")
    name = arg(i, a, 1, ("string",), "Message.from", n, None)
    if name:
        if re.search(r"[\r\n]", name):
            i.throw("MailException", "Message.from(): name contains a line break", n)
        addr = email.utils.formataddr((name, addr))
    this.native.sender = addr
    return this


def msg_str(field, fname):
    def fn(i, a, n, this):
        value = need(i, a[0], ("string",), fname, 1, n)
        if field == "subject" and re.search(r"[\r\n]", value):
            i.throw("MailException", "Message.subject(): subject contains a line break", n)
        setattr(this.native, field, value)
        return this
    return fn


def msg_header(i, a, n, this):
    name = need(i, a[0], ("string",), "Message.header", 1, n)
    value = need(i, a[1], ("string",), "Message.header", 2, n)
    if not re.match(r"^[A-Za-z0-9-]+$", name) or re.search(r"[\r\n]", value):
        i.throw("MailException", f'Message.header(): invalid header "{name}"', n)
    this.native.headers[name] = value
    return this


def msg_attach(i, a, n, this):
    path = need(i, a[0], ("string",), "Message.attach", 1, n)
    full = path if os.path.isabs(path) else os.path.join(i.root, path)
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError as e:
        i.throw("IOException", f'Message.attach(): cannot read "{path}": {e.strerror or e}', n)
    name = arg(i, a, 1, ("string",), "Message.attach", n, os.path.basename(path))
    this.native.attachments.append((name, mimetypes.guess_type(name)[0] or "application/octet-stream", data))
    return this


def msg_attach_data(i, a, n, this):
    data = need(i, a[0], ("string",), "Message.attachData", 1, n).encode("utf-8")
    name = need(i, a[1], ("string",), "Message.attachData", 2, n)
    mime = arg(i, a, 2, ("string",), "Message.attachData", n, mimetypes.guess_type(name)[0] or "text/plain")
    this.native.attachments.append((name, mime, data))
    return this


MESSAGE_METHODS = {
    "to": (msg_list("to", "Message.to"), 1, 1),
    "cc": (msg_list("cc", "Message.cc"), 1, 1),
    "bcc": (msg_list("bcc", "Message.bcc"), 1, 1),
    "replyTo": (msg_list("reply_to", "Message.replyTo"), 1, 1),
    "from": (msg_from, 1, 2),
    "subject": (msg_str("subject", "Message.subject"), 1, 1),
    "text": (msg_str("text", "Message.text"), 1, 1),
    "html": (msg_str("html", "Message.html"), 1, 1),
    "header": (msg_header, 2, 2),
    "attach": (msg_attach, 1, 2),
    "attachData": (msg_attach_data, 2, 3),
}


def build_message(i, content, recipients, n, fname):
    import email.utils
    """Run a mailable (object with build(Message)) or a function on a new Message.
    -> a plain dict that can be sent now or stored in a queue."""
    msg = new_native(i, "Message", MessageState())
    st = msg.native
    st.to, st.cc, st.bcc = list(recipients[0]), list(recipients[1]), list(recipients[2])
    if is_callable(content):
        i.call_function(content, [msg], n)
    elif isinstance(content, BarqObject) and content.cls.find_method("build") is not None:
        i.call_function(BoundMethod(content, content.cls.find_method("build")), [msg], n)
    else:
        i.throw("TypeException", f"{fname}() needs a mailable (an object with a build(Message m) method) "
                f"or a function, got {type_name(content)}", n)
    if not (st.to or st.cc or st.bcc):
        i.throw("MailException", f"{fname}(): the message has no recipients", n)
    if st.text is None and st.html is None:
        i.throw("MailException", f"{fname}(): the message has no body — call text() or html()", n)
    cfg = mail_state(i).settings
    sender = st.sender
    if sender is None:
        sender = cfg.get("from") or "noreply@localhost"
        if cfg.get("fromName"):
            sender = email.utils.formataddr((cfg["fromName"], sender))
    return {"to": st.to, "cc": st.cc, "bcc": st.bcc, "replyTo": st.reply_to, "from": sender, "subject": st.subject,
            "text": st.text, "html": st.html, "headers": dict(st.headers),
            "attachments": [{"name": nm, "mime": mt, "data": base64.b64encode(d).decode()} for nm, mt, d in st.attachments]}


def email_message(m):
    import email.utils
    from email.message import EmailMessage
    em = EmailMessage()
    em["From"] = m["from"]
    if m["to"]:
        em["To"] = ", ".join(m["to"])
    if m["cc"]:
        em["Cc"] = ", ".join(m["cc"])
    if m["replyTo"]:
        em["Reply-To"] = ", ".join(m["replyTo"])
    em["Subject"] = m["subject"]
    em["Date"] = email.utils.formatdate(localtime=True)
    em["Message-ID"] = email.utils.make_msgid(domain=m["from"].rpartition("@")[2].strip(">") or "localhost")
    for k, v in m["headers"].items():
        em[k] = v
    if m["text"] is not None:
        em.set_content(m["text"])
        if m["html"] is not None:
            em.add_alternative(m["html"], subtype="html")
    else:
        em.set_content(m["html"], subtype="html")
    for att in m["attachments"]:
        main, _, sub = att["mime"].partition("/")
        em.add_attachment(base64.b64decode(att["data"]), maintype=main, subtype=sub or "octet-stream", filename=att["name"])
    return em


def deliver(i, m, n):
    import email.utils
    import smtplib
    import ssl
    st = mail_state(i)
    cfg = st.settings
    driver = cfg["driver"]
    if driver == "array":
        st.sent.append({"to": list(m["to"]), "cc": list(m["cc"]), "bcc": list(m["bcc"]), "from": m["from"],
                        "subject": m["subject"], "text": m["text"], "html": m["html"],
                        "attachments": [a["name"] for a in m["attachments"]]})
        return None
    try:
        em = email_message(m)
    except (ValueError, TypeError) as e:
        i.throw("MailException", f"cannot build the message: {e}", n)
    if driver == "log":
        sys.stdout.flush()
        body = (m["text"] if m["text"] is not None else m["html"]).rstrip()
        lines = [f"[mail] From: {m['from']}", f"[mail] To: {', '.join(m['to'] + m['cc'] + m['bcc'])}",
                 f"[mail] Subject: {m['subject']}"] + ["[mail]   " + ln for ln in body.splitlines()]
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()
        return None
    if driver == "file":
        folder = cfg["path"] if os.path.isabs(cfg["path"]) else os.path.join(i.root, cfg["path"])
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1e6) % 1000000:06d}.eml"
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), "wb") as f:
                f.write(em.as_bytes())
        except OSError as e:
            i.throw("MailException", f"cannot write mail to {folder}: {e.strerror or e}", n)
        return None
    # smtp
    host, port = cfg["host"], cfg["port"]
    enc = (cfg.get("encryption") or "none").lower()
    timeout = cfg.get("timeout") or 30
    rcpts = [email.utils.parseaddr(x)[1] for x in m["to"] + m["cc"] + m["bcc"]]
    try:
        if enc == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
        with smtp:
            if enc == "tls":
                smtp.starttls(context=ssl.create_default_context())
            if cfg.get("username"):
                smtp.login(cfg["username"], cfg.get("password") or "")
            smtp.send_message(em, from_addr=email.utils.parseaddr(m["from"])[1], to_addrs=rcpts)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        i.throw("MailException", f"sending mail via {host}:{port} failed: {e}", n)
    return None


class PendingState:
    def __init__(self, to):
        self.to, self.cc, self.bcc = to, [], []


def pending_recipients(this):
    p = this.native
    return p.to, p.cc, p.bcc


def pm_add(field, fname):
    def fn(i, a, n, this):
        getattr(this.native, field).extend(addresses(i, a[0], n, fname))
        return this
    return fn


def pm_send(i, a, n, this):
    return deliver(i, build_message(i, a[0], pending_recipients(this), n, "Mail.send"), n)


def pm_queue(i, a, n, this):
    m = build_message(i, a[0], pending_recipients(this), n, "Mail.queue")
    return queue_push(i, {"__mail": m}, arg(i, a, 1, ("string",), "Mail.queue", n, None), 0, n)


def pm_later(i, a, n, this):
    delay = need(i, a[0], ("int",), "Mail.later", 1, n)
    m = build_message(i, a[1], pending_recipients(this), n, "Mail.later")
    return queue_push(i, {"__mail": m}, None, delay, n)


PENDING_METHODS = {
    "cc": (pm_add("cc", "PendingMail.cc"), 1, 1),
    "bcc": (pm_add("bcc", "PendingMail.bcc"), 1, 1),
    "send": (pm_send, 1, 1),
    "queue": (pm_queue, 1, 2),
    "later": (pm_later, 2, 2),
}


def mail_raw(i, a, n):
    text = need(i, a[0], ("string",), "Mail.raw", 1, n)
    fn = a[1]
    if not is_callable(fn):
        i.throw("TypeException", f"Mail.raw() needs a function as argument 2, got {type_name(fn)}", n)

    def build(i2, args, n2):
        args[0].native.text = text
        return i.call_function(fn, args, n2)
    return deliver(i, build_message(i, NativeFunction("Mail.raw", build, 1, 1), ([], [], []), n, "Mail.raw"), n)


def mail_fake(i, a, n):
    st = mail_state(i)
    st.settings["driver"] = "array"
    st.sent = []
    return None


MAIL = {
    "to": (lambda i, a, n: new_native(i, "PendingMail", PendingState(addresses(i, a[0], n, "Mail.to"))), 1, 1),
    "send": (lambda i, a, n: deliver(i, build_message(i, a[0], ([], [], []), n, "Mail.send"), n), 1, 1),
    "raw": (mail_raw, 2, 2),
    "config": (mail_config, 1, 1),
    "fake": (mail_fake, 0, 0),
    "sent": (lambda i, a, n: [dict(m) for m in mail_state(i).sent], 0, 0),
    "driver": (lambda i, a, n: mail_state(i).settings["driver"], 0, 0),
}


# ===========================================================================
# Queue
# ===========================================================================

QUEUE_DEFAULTS = {"driver": "sync", "path": "storage/queue.db", "queue": "default"}
QUEUE_DRIVERS = ("sync", "memory", "database")


class QueueState:
    def __init__(self, settings):
        self.settings = settings
        self.jobs = []          # memory driver: dicts
        self.failed = []        # memory driver
        self.next_id = 1
        self.conn = None        # database driver
        self.fake = None        # list of pushed jobs after Queue.fake()


def queue_state(i):
    st = getattr(i, "_queue", None)
    if st is None:
        cfg = dict(QUEUE_DEFAULTS)
        if os.environ.get("QUEUE_CONNECTION"):
            cfg["driver"] = os.environ["QUEUE_CONNECTION"]
        conf = config_lookup(i, "queue")
        if isinstance(conf, dict):
            cfg.update({k: v for k, v in conf.items() if k in QUEUE_DEFAULTS})
        st = i._queue = QueueState(cfg)
    return st


def queue_config(i, a, n):
    opts = need(i, a[0], ("dict",), "Queue.config", 1, n)
    st = queue_state(i)
    for k, v in opts.items():
        if k not in QUEUE_DEFAULTS:
            i.throw("ValueException", f'Queue.config(): unknown option "{k}" (use driver, path, queue)', n)
        need(i, v, ("string",), f"Queue.config option {k}", 1, n)
        st.settings[k] = v
    if st.settings["driver"] not in QUEUE_DRIVERS:
        i.throw("ValueException", f'Queue.config(): driver must be "sync", "memory" or "database", got "{st.settings["driver"]}"', n)
    if st.conn is not None:
        st.conn.close()
        st.conn = None
    return None


def queue_db(i, n):
    st = queue_state(i)
    if st.conn is None:
        path = st.settings["path"]
        if path != ":memory:" and not os.path.isabs(path):
            path = os.path.join(i.root, path)
        try:
            if path != ":memory:":
                os.makedirs(os.path.dirname(path), exist_ok=True)
            st.conn = sqlite3.connect(path, timeout=10, isolation_level=None)
            st.conn.executescript(
                "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL, "
                "payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, "
                "reserved_at REAL, created_at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (queue, available_at);"
                "CREATE TABLE IF NOT EXISTS failed_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL, "
                "payload TEXT NOT NULL, error TEXT NOT NULL, failed_at REAL NOT NULL);")
        except (sqlite3.Error, OSError) as e:
            i.throw("QueueException", f"cannot open the queue database {path}: {e}", n)
    return st.conn


# ---- storing jobs in the database --------------------------------------------

def serialize(i, v, n, where="job"):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        return [serialize(i, x, n, where) for x in v]
    if isinstance(v, dict):
        return {"__dict": [[serialize(i, k, n, where), serialize(i, x, n, where)] for k, x in v.items()]}
    if isinstance(v, BarqSet):
        return {"__set": [serialize(i, x, n, where) for x in v.values()]}
    if isinstance(v, BarqObject) and v.cls.is_enum:
        return {"__enum": v.cls.fqn, "case": v.fields["name"]}
    if isinstance(v, BarqObject) and not v.cls.native:
        return {"__obj": v.cls.fqn, "fields": {k: serialize(i, x, n, f"{where}.{k}") for k, x in v.fields.items()}}
    i.throw("QueueException", f"cannot store {type_name(v)} in a queued job ({where}) — the database queue keeps "
            "ints, floats, strings, bools, lists, dicts, sets, enums and your own objects", n)


def find_class(i, fqn, n):
    cls = i.classes.get(fqn)
    if cls is None:
        try:
            cls = i.lookup_fqn(fqn, n, None, required=False)
        except BarqThrow:
            cls = None
    if not isinstance(cls, BarqClass):
        i.throw("QueueException", f"queued job needs class {fqn}, which can't be found — job classes must live in their own "
                "namespaced file (e.g. app/Jobs/SendEmail.bq) so a worker can load them", n)
    return cls


def deserialize(i, v, n):
    if isinstance(v, list):
        return [deserialize(i, x, n) for x in v]
    if isinstance(v, dict):
        if "__dict" in v:
            return {deserialize(i, k, n): deserialize(i, x, n) for k, x in v["__dict"]}
        if "__set" in v:
            return BarqSet([deserialize(i, x, n) for x in v["__set"]])
        if "__enum" in v:
            cls = find_class(i, v["__enum"], n)
            return cls.cases[v["case"]]
        if "__obj" in v:
            obj = i.instantiate_bare(find_class(i, v["__obj"], n))
            obj.fields.update({k: deserialize(i, x, n) for k, x in v["fields"].items()})
            return obj
        if "__mail" in v:
            return v
    return v


def job_label(job):
    if isinstance(job, dict) and "__mail" in job:
        return "Mail: " + (job["__mail"].get("subject") or "(no subject)")
    if isinstance(job, BarqObject):
        return job.cls.name
    return "function"


def job_setting(job, name, default):
    if isinstance(job, BarqObject) and type(job.fields.get(name)) is int:
        return job.fields[name]
    return default


def check_job(i, job, n, fname):
    if isinstance(job, dict) and "__mail" in job:
        return job
    if is_callable(job):
        return job
    if isinstance(job, BarqObject) and job.cls.find_method("handle") is not None:
        return job
    i.throw("TypeException", f"{fname}() needs a job — an object with a handle() method, or a function — got {type_name(job)}", n)


def run_job(i, job, n):
    if isinstance(job, dict) and "__mail" in job:
        return deliver(i, job["__mail"], n)
    if is_callable(job):
        return i.call_function(job, [], n)
    return i.call_function(BoundMethod(job, job.cls.find_method("handle")), [], n)


def queue_push(i, job, queue, delay, n, fname="Queue.push"):
    job = check_job(i, job, n, fname)
    st = queue_state(i)
    queue = queue or st.settings["queue"]
    if delay < 0:
        i.throw("ValueException", f"{fname}(): delay must be 0 or more seconds, got {delay}", n)
    if st.fake is not None:
        st.fake.append({"job": job_label(job), "queue": queue, "delay": delay})
        return len(st.fake)
    driver = st.settings["driver"]
    if driver == "sync":
        run_job(i, job, n)
        return None
    now = time.time()
    if driver == "memory":
        jid = st.next_id
        st.next_id += 1
        st.jobs.append({"id": jid, "queue": queue, "job": job, "attempts": 0, "available_at": now + delay})
        return jid
    payload = json.dumps(job if isinstance(job, dict) else serialize(i, job, n), ensure_ascii=False) \
        if not is_callable(job) else None
    if payload is None:
        i.throw("QueueException", f"{fname}(): functions can't be stored in the database queue — use a job class", n)
    cur = queue_db(i, n).execute("INSERT INTO jobs (queue, payload, available_at, created_at) VALUES (?, ?, ?, ?)",
                                 (queue, payload, now + delay, now))
    return cur.lastrowid


def reserve(i, queues, n):
    """Take the next due job: -> (record dict, job) or None."""
    st = queue_state(i)
    now = time.time()
    if st.settings["driver"] == "memory":
        for rec in st.jobs:
            if rec["queue"] in queues and rec["available_at"] <= now and not rec.get("reserved"):
                rec["reserved"] = True
                rec["attempts"] += 1
                return rec, rec["job"]
        return None
    db = queue_db(i, n)
    marks = ",".join("?" * len(queues))
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"SELECT id, queue, payload, attempts FROM jobs WHERE queue IN ({marks}) AND available_at <= ? "
                         f"AND (reserved_at IS NULL OR reserved_at < ?) ORDER BY id LIMIT 1",
                         (*queues, now, now - 600)).fetchone()
        if row is not None:
            db.execute("UPDATE jobs SET reserved_at = ?, attempts = attempts + 1 WHERE id = ?", (now, row[0]))
        db.execute("COMMIT")
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        i.throw("QueueException", f"queue database error: {e}", n)
    if row is None:
        return None
    rec = {"id": row[0], "queue": row[1], "payload": row[2], "attempts": row[3] + 1}
    return rec, deserialize(i, json.loads(row[2]), n)


def job_done(i, rec, n):
    st = queue_state(i)
    if st.settings["driver"] == "memory":
        st.jobs.remove(rec)
    else:
        queue_db(i, n).execute("DELETE FROM jobs WHERE id = ?", (rec["id"],))


def job_failed(i, rec, job, error, n):
    """-> True when the job will be retried."""
    st = queue_state(i)
    tries = job_setting(job, "tries", 1)
    if rec["attempts"] < tries:
        later = time.time() + job_setting(job, "backoff", 0)
        if st.settings["driver"] == "memory":
            rec["reserved"] = False
            rec["available_at"] = later
        else:
            queue_db(i, n).execute("UPDATE jobs SET reserved_at = NULL, available_at = ? WHERE id = ?", (later, rec["id"]))
        return True
    job_done(i, rec, n)
    message = f"{error.cls.name}: {error.fields.get('message', '')}"
    if st.settings["driver"] == "memory":
        st.failed.append({"id": rec["id"], "queue": rec["queue"], "job": job, "error": message, "failedAt": int(time.time())})
    else:
        queue_db(i, n).execute("INSERT INTO failed_jobs (queue, payload, error, failed_at) VALUES (?, ?, ?, ?)",
                               (rec["queue"], rec["payload"], message, time.time()))
    if isinstance(job, BarqObject) and job.cls.find_method("failed") is not None:
        i.call_function(BoundMethod(job, job.cls.find_method("failed")), [error], n)
    return False


WORK_OPTIONS = ("queue", "max", "stopWhenEmpty", "sleep", "quiet")


def queue_work(i, a, n, fixed_queue=None):
    opts = arg(i, a, 0, ("dict",), "Queue.work", n, {})
    for k in opts:
        if k not in WORK_OPTIONS:
            i.throw("ValueException", f'Queue.work(): unknown option "{k}" (use {", ".join(WORK_OPTIONS)})', n)
    st = queue_state(i)
    if st.settings["driver"] == "sync":
        return 0
    q = fixed_queue or opts.get("queue", st.settings["queue"])
    queues = [need(i, x, ("string",), "Queue.work option queue", 1, n) for x in (q if isinstance(q, list) else [q])]
    limit = opts.get("max")
    stop_empty = opts.get("stopWhenEmpty", True)
    pause = need(i, opts.get("sleep", 1), ("int", "float"), "Queue.work option sleep", 1, n)
    quiet = opts.get("quiet", False)
    done = 0
    try:
        while limit is None or done < limit:
            got = reserve(i, queues, n)
            if got is None:
                if stop_empty:
                    break
                time.sleep(pause)
                continue
            rec, job = got
            start = time.perf_counter()
            try:
                run_job(i, job, n)
            except BarqThrow as t:
                retry = job_failed(i, rec, job, t.obj, n)
                if not quiet:
                    sys.stdout.flush()
                    sys.stderr.write(f"  [queue] {job_label(job)} #{rec['id']} failed"
                                     f"{' (will retry)' if retry else ''}: {t.obj.fields.get('message', '')}\n")
            else:
                job_done(i, rec, n)
                if not quiet:
                    ms = (time.perf_counter() - start) * 1000
                    sys.stdout.flush()
                    sys.stderr.write(f"  [queue] {job_label(job)} #{rec['id']} done ({ms:.1f} ms)\n")
            done += 1
    except KeyboardInterrupt:
        pass
    return done


def queue_size(i, a, n, fixed_queue=None):
    st = queue_state(i)
    q = fixed_queue or arg(i, a, 0, ("string",), "Queue.size", n, st.settings["queue"])
    if st.settings["driver"] == "memory":
        return sum(1 for r in st.jobs if r["queue"] == q)
    if st.settings["driver"] == "sync":
        return 0
    return queue_db(i, n).execute("SELECT COUNT(*) FROM jobs WHERE queue = ?", (q,)).fetchone()[0]


def queue_clear(i, a, n, fixed_queue=None):
    st = queue_state(i)
    q = fixed_queue or arg(i, a, 0, ("string",), "Queue.clear", n, st.settings["queue"])
    if st.settings["driver"] == "memory":
        before = len(st.jobs)
        st.jobs = [r for r in st.jobs if r["queue"] != q]
        return before - len(st.jobs)
    if st.settings["driver"] == "sync":
        return 0
    return queue_db(i, n).execute("DELETE FROM jobs WHERE queue = ?", (q,)).rowcount


def queue_failed(i, a, n):
    st = queue_state(i)
    if st.settings["driver"] == "memory":
        return [{"id": f["id"], "queue": f["queue"], "job": job_label(f["job"]), "error": f["error"],
                 "failedAt": f["failedAt"]} for f in st.failed]
    if st.settings["driver"] == "sync":
        return []
    rows = queue_db(i, n).execute("SELECT id, queue, payload, error, failed_at FROM failed_jobs ORDER BY id").fetchall()
    out = []
    for jid, q, payload, error, at in rows:
        data = json.loads(payload)
        label = data.get("__obj") or ("Mail" if "__mail" in data else "job")
        out.append({"id": jid, "queue": q, "job": label.rpartition("\\")[2], "error": error, "failedAt": int(at)})
    return out


def queue_retry(i, a, n):
    """Queue.retry(id) or Queue.retry("all") -> number of jobs put back."""
    target = a[0]
    st = queue_state(i)
    every = target == "all"
    if not every:
        need(i, target, ("int",), "Queue.retry", 1, n)
    now = time.time()
    if st.settings["driver"] == "memory":
        back = [f for f in st.failed if every or f["id"] == target]
        for f in back:
            st.failed.remove(f)
            st.jobs.append({"id": f["id"], "queue": f["queue"], "job": f["job"], "attempts": 0, "available_at": now})
        return len(back)
    if st.settings["driver"] == "sync":
        return 0
    db = queue_db(i, n)
    rows = db.execute("SELECT id, queue, payload FROM failed_jobs" + ("" if every else " WHERE id = ?"),
                      () if every else (target,)).fetchall()
    for fid, q, payload in rows:
        db.execute("INSERT INTO jobs (queue, payload, available_at, created_at) VALUES (?, ?, ?, ?)", (q, payload, now, now))
        db.execute("DELETE FROM failed_jobs WHERE id = ?", (fid,))
    return len(rows)


def queue_flush_failed(i, a, n):
    st = queue_state(i)
    if st.settings["driver"] == "memory":
        count = len(st.failed)
        st.failed = []
        return count
    if st.settings["driver"] == "sync":
        return 0
    return queue_db(i, n).execute("DELETE FROM failed_jobs").rowcount


def queue_fake(i, a, n):
    queue_state(i).fake = []
    return None


def queue_pushed(i, a, n):
    st = queue_state(i)
    if st.fake is None:
        i.throw("QueueException", "Queue.pushed() only works after Queue.fake()", n)
    return [dict(x) for x in st.fake]


class JobQueueState:
    def __init__(self, name):
        self.name = name


JOB_QUEUE_METHODS = {
    "push": (lambda i, a, n, this: queue_push(i, a[0], this.native.name, 0, n, "JobQueue.push"), 1, 1),
    "later": (lambda i, a, n, this: queue_push(i, a[1], this.native.name, need(i, a[0], ("int",), "JobQueue.later", 1, n),
                                               n, "JobQueue.later"), 2, 2),
    "size": (lambda i, a, n, this: queue_size(i, [], n, this.native.name), 0, 0),
    "clear": (lambda i, a, n, this: queue_clear(i, [], n, this.native.name), 0, 0),
    "work": (lambda i, a, n, this: queue_work(i, a, n, this.native.name), 0, 1),
    "name": (lambda i, a, n, this: this.native.name, 0, 0),
}

QUEUE = {
    "push": (lambda i, a, n: queue_push(i, a[0], arg(i, a, 1, ("string",), "Queue.push", n, None), 0, n), 1, 2),
    "later": (lambda i, a, n: queue_push(i, a[1], arg(i, a, 2, ("string",), "Queue.later", n, None),
                                         need(i, a[0], ("int",), "Queue.later", 1, n), n, "Queue.later"), 2, 3),
    "on": (lambda i, a, n: new_native(i, "JobQueue", JobQueueState(need(i, a[0], ("string",), "Queue.on", 1, n))), 1, 1),
    "work": (queue_work, 0, 1),
    "size": (queue_size, 0, 1),
    "clear": (queue_clear, 0, 1),
    "failed": (queue_failed, 0, 0),
    "retry": (queue_retry, 1, 1),
    "flushFailed": (queue_flush_failed, 0, 0),
    "config": (queue_config, 1, 1),
    "driver": (lambda i, a, n: queue_state(i).settings["driver"], 0, 0),
    "fake": (queue_fake, 0, 0),
    "pushed": (queue_pushed, 0, 0),
}


# ===========================================================================
# Cli
# ===========================================================================

COLORS = {"black": 30, "red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
          "gray": 90, "grey": 90}


def use_color(stream):
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text, code, stream):
    return f"\033[{code}m{text}\033[0m" if use_color(stream) else text


def parse_argv(argv):
    """-> (positional list, options dict). --a=b, --flag, --no-flag, -abc, '--' ends options."""
    pos, opts = [], {}
    rest = False
    for a in argv:
        if rest or a == "-" or not a.startswith("-"):
            pos.append(a)
        elif a == "--":
            rest = True
        elif a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            if eq:
                opts[name] = value
            elif name.startswith("no-"):
                opts[name[3:]] = False
            else:
                opts[name] = True
        else:
            body = a[1:]
            if "=" in body:
                name, _, value = body.partition("=")
                opts[name] = value
            else:
                for ch in body:
                    opts[ch] = True
    return pos, opts


def cli_args(i):
    return parse_argv(list(i.argv))


def cli_option(i, a, n):
    name = need(i, a[0], ("string",), "Cli.option", 1, n)
    return cli_args(i)[1].get(name, a[1] if len(a) > 1 else None)


def cli_flag(i, a, n):
    v = cli_args(i)[1].get(need(i, a[0], ("string",), "Cli.flag", 1, n), False)
    return v is True or (isinstance(v, str) and v.lower() in ("1", "true", "yes", "on"))


def cli_out(code, stream_name="stdout"):
    def fn(i, a, n):
        text = to_display(a[0], i) if a else ""
        if stream_name == "stderr":
            i.stdout.flush()
            sys.stderr.write(paint(text, code, sys.stderr) + "\n")
            sys.stderr.flush()
        else:
            i.stdout.write((paint(text, code, sys.stdout) if code else text) + "\n")
        return None
    return fn


def cli_color(i, a, n):
    text = to_display(a[0], i)
    name = need(i, a[1], ("string",), "Cli.color", 2, n)
    if name not in COLORS:
        i.throw("ValueException", f'Cli.color(): unknown color "{name}" (use {", ".join(COLORS)})', n)
    return paint(text, COLORS[name], sys.stdout)


def read_line(i, prompt):
    i.stdout.write(prompt)
    i.stdout.flush()
    line = sys.stdin.readline()
    if line == "":
        return None
    return line.rstrip("\r\n")


def cli_ask(i, a, n):
    q = need(i, a[0], ("string",), "Cli.ask", 1, n)
    default = arg(i, a, 1, ("string",), "Cli.ask", n, None)
    line = read_line(i, q + (f" [{default}]" if default else "") + " ")
    if line is None or line.strip() == "":
        return default if default is not None else ""
    return line


def cli_confirm(i, a, n):
    q = need(i, a[0], ("string",), "Cli.confirm", 1, n)
    default = arg(i, a, 1, ("bool",), "Cli.confirm", n, False)
    while True:
        line = read_line(i, q + (" [Y/n] " if default else " [y/N] "))
        if line is None or line.strip() == "":
            return default
        ans = line.strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def cli_secret(i, a, n):
    q = need(i, a[0], ("string",), "Cli.secret", 1, n)
    if sys.stdin.isatty():
        import getpass
        i.stdout.flush()
        try:
            return getpass.getpass(q + " ")
        except EOFError:
            return ""
    return read_line(i, q + " ") or ""


def cli_choice(i, a, n):
    q = need(i, a[0], ("string",), "Cli.choice", 1, n)
    options = [to_display(x, i) for x in need(i, a[1], ("list",), "Cli.choice", 2, n)]
    if not options:
        i.throw("ValueException", "Cli.choice() needs at least one option", n)
    default = arg(i, a, 2, ("string",), "Cli.choice", n, None)
    i.stdout.write(q + "\n")
    for k, opt in enumerate(options, 1):
        i.stdout.write(f"  {k}) {opt}{' (default)' if opt == default else ''}\n")
    while True:
        line = read_line(i, "> ")
        if line is None or line.strip() == "":
            if default is not None:
                return default
            if line is None:
                i.throw("IOException", "Cli.choice(): no input", n)
            continue
        s = line.strip()
        if s.isdigit() and 1 <= int(s) <= len(options):
            return options[int(s) - 1]
        if s in options:
            return s


def cli_table(i, a, n):
    headers = [to_display(h, i) for h in need(i, a[0], ("list",), "Cli.table", 1, n)]
    rows = []
    for r in need(i, a[1], ("list",), "Cli.table", 2, n):
        if isinstance(r, dict):
            rows.append([to_display(r.get(h), i) if r.get(h) is not None else "" for h in headers])
        elif isinstance(r, list):
            rows.append([to_display(x, i) if x is not None else "" for x in r])
        else:
            i.throw("TypeException", f"Cli.table(): each row must be a list or dict, got {type_name(r)}", n)
    widths = [len(h) for h in headers]
    for r in rows:
        for k, cell in enumerate(r[:len(headers)]):
            widths[k] = max(widths[k], len(cell))
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(cells):
        cells = list(cells) + [""] * (len(headers) - len(cells))
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    out = [line, fmt(headers), line] + [fmt(r[:len(headers)]) for r in rows] + [line]
    i.stdout.write("\n".join(out) + "\n")
    return None


class CliCommands:
    def __init__(self):
        self.commands = {}


def cli_command(i, a, n):
    name = need(i, a[0], ("string",), "Cli.command", 1, n)
    desc = need(i, a[1], ("string",), "Cli.command", 2, n)
    fn = a[2]
    if not is_callable(fn):
        i.throw("TypeException", f"Cli.command() needs a function as argument 3, got {type_name(fn)}", n)
    cmds = i.__dict__.setdefault("_cli", CliCommands()).commands
    cmds[name] = (desc, fn)
    return None


def cli_run(i, a, n):
    cmds = i.__dict__.setdefault("_cli", CliCommands()).commands
    pos, opts = cli_args(i)
    title = arg(i, a, 0, ("string",), "Cli.run", n, "Commands")
    if not pos or pos[0] in ("help", "list") or opts.get("help") is True or opts.get("h") is True:
        i.stdout.write(f"{title}:\n")
        width = max((len(k) for k in cmds), default=0)
        for name, (desc, _) in cmds.items():
            i.stdout.write(f"  {paint(name.ljust(width), 32, sys.stdout)}  {desc}\n")
        return 0 if (pos and pos[0] in ("help", "list")) or opts.get("help") or opts.get("h") else 1
    name = pos[0]
    if name not in cmds:
        i.stdout.flush()
        sys.stderr.write(f'Unknown command "{name}". Run with "help" to see the commands.\n')
        return 1
    fn = cmds[name][1]
    from .web import fn_params
    params = fn_params(fn)
    result = i.call_function(fn, [pos[1:]] if params is None or len(params) == 1 else [], n)
    return result if type(result) is int else 0


CLI = {
    "args": (lambda i, a, n: cli_args(i)[0], 0, 0),
    "options": (lambda i, a, n: cli_args(i)[1], 0, 0),
    "option": (cli_option, 1, 2),
    "flag": (cli_flag, 1, 1),
    "hasOption": (lambda i, a, n: need(i, a[0], ("string",), "Cli.hasOption", 1, n) in cli_args(i)[1], 1, 1),
    "line": (cli_out(None), 0, 1),
    "info": (cli_out(36), 1, 1),
    "success": (cli_out(32), 1, 1),
    "warn": (cli_out(33, "stderr"), 1, 1),
    "error": (cli_out(31, "stderr"), 1, 1),
    "color": (cli_color, 2, 2),
    "bold": (lambda i, a, n: paint(to_display(a[0], i), 1, sys.stdout), 1, 1),
    "ask": (cli_ask, 1, 2),
    "confirm": (cli_confirm, 1, 2),
    "secret": (cli_secret, 1, 1),
    "choice": (cli_choice, 2, 3),
    "table": (cli_table, 2, 2),
    "command": (cli_command, 3, 3),
    "run": (cli_run, 0, 1),
}


# ===========================================================================
# Test
# ===========================================================================

class TestCase:
    def __init__(self, name, groups, fn, skip, before, after):
        self.name = name
        self.groups = groups
        self.fn = fn
        self.skip = skip
        self.before = before
        self.after = after


class TestState:
    def __init__(self):
        self.cases = []
        self.groups = []        # describe() stack: (name, before hooks, after hooks)
        self.root_before = []
        self.root_after = []
        self.totals = [0, 0, 0]  # passed, failed, skipped (across Test.run() calls)
        self.failures = []


def test_state(i):
    st = getattr(i, "_tests", None)
    if st is None:
        st = i._tests = TestState()
    return st


def hooks(st):
    before = list(st.root_before) + [h for g in st.groups for h in g[1]]
    after = [h for g in reversed(st.groups) for h in g[2]] + list(st.root_after)
    return before, after


def add_test(skip):
    fname = "Test.skip" if skip else "Test.it"

    def fn(i, a, n):
        name = need(i, a[0], ("string",), fname, 1, n)
        body = a[1] if len(a) > 1 else None
        if body is not None and not is_callable(body):
            i.throw("TypeException", f"{fname}() needs a function as argument 2, got {type_name(body)}", n)
        if body is None and not skip:
            i.throw("TypeException", f"{fname}() needs a function as argument 2", n)
        st = test_state(i)
        before, after = hooks(st)
        st.cases.append(TestCase(name, [g[0] for g in st.groups], body, skip, before, after))
        return None
    return fn


def test_describe(i, a, n):
    name = need(i, a[0], ("string",), "Test.describe", 1, n)
    body = a[1]
    if not is_callable(body):
        i.throw("TypeException", f"Test.describe() needs a function as argument 2, got {type_name(body)}", n)
    st = test_state(i)
    st.groups.append((name, [], []))
    try:
        i.call_function(body, [], n)
    finally:
        st.groups.pop()
    return None


def add_hook(where):
    def fn(i, a, n):
        body = a[0]
        if not is_callable(body):
            i.throw("TypeException", f"Test.{where}() needs a function, got {type_name(body)}", n)
        st = test_state(i)
        if st.groups:
            st.groups[-1][1 if where == "beforeEach" else 2].append(body)
        else:
            (st.root_before if where == "beforeEach" else st.root_after).append(body)
        return None
    return fn


def location(obj):
    if obj.origin:
        source, line, _ = obj.origin
        path = source.path if source is not None else "<unknown>"
        return f"{path}:{line}"
    return ""


def describe_failure(i, t):
    obj = t.obj
    msg = to_display(obj.fields.get("message", ""), i)
    where = location(obj)
    head = msg if obj.cls.name == "AssertionException" else f"{obj.cls.name}: {msg}"
    return head, where


def run_tests(i, n=None):
    """Run the registered tests; print a report. -> (passed, failed, skipped)."""
    st = test_state(i)
    cases, st.cases = st.cases, []
    out = i.stdout
    color = use_color(sys.stdout)
    green, red, yellow, dim, reset = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m") if color else ("",) * 5
    passed = failed = skipped = 0
    shown = []
    for case in cases:
        # print describe() headings when the group changes
        common = 0
        while common < min(len(shown), len(case.groups)) and shown[common] == case.groups[common]:
            common += 1
        for depth in range(common, len(case.groups)):
            out.write("  " * depth + case.groups[depth] + "\n")
        shown = case.groups
        pad = "  " * (len(case.groups) + 1)
        if case.skip:
            skipped += 1
            out.write(f"{pad}{yellow}-{reset} {case.name} {dim}(skipped){reset}\n")
            continue
        error = None
        try:
            for h in case.before:
                i.call_function(h, [], n)
            i.call_function(case.fn, [], n)
        except BarqThrow as t:
            error = t
        finally:
            for h in case.after:
                try:
                    i.call_function(h, [], n)
                except BarqThrow as t:
                    error = error or t
        if error is None:
            passed += 1
            out.write(f"{pad}{green}✓{reset} {case.name}\n")
        else:
            failed += 1
            head, where = describe_failure(i, error)
            out.write(f"{pad}{red}✗ {case.name}{reset}\n")
            for line in head.splitlines():
                out.write(f"{pad}    {line}\n")
            if where:
                out.write(f"{pad}    {dim}at {where}{reset}\n")
            st.failures.append((" › ".join(case.groups + [case.name]), head, where))
    st.totals[0] += passed
    st.totals[1] += failed
    st.totals[2] += skipped
    return passed, failed, skipped


def summary_line(passed, failed, skipped, color):
    green, red, yellow, reset = ("\033[32m", "\033[31m", "\033[33m", "\033[0m") if color else ("",) * 4
    parts = [f"{green}{passed} passed{reset}"]
    if failed:
        parts.append(f"{red}{failed} failed{reset}")
    if skipped:
        parts.append(f"{yellow}{skipped} skipped{reset}")
    return ", ".join(parts)


def test_run(i, a, n):
    p, f, s = run_tests(i, n)
    i.stdout.write(summary_line(p, f, s, use_color(sys.stdout)) + "\n")
    return f == 0


# ---- assertions ----------------------------------------------------------------

def fail(i, message, n, custom=None):
    if custom is not None:
        message = f"{to_display(custom, i)}\n{message}"
    i.throw("AssertionException", message, n)


def msg_arg(a, k):
    return a[k] if len(a) > k else None


def a_equals(i, a, n):
    if not values_equal(a[0], a[1]):
        fail(i, f"expected {show(i, a[0])}, got {show(i, a[1])}", n, msg_arg(a, 2))
    return None


def a_not_equals(i, a, n):
    if values_equal(a[0], a[1]):
        fail(i, f"expected anything but {show(i, a[0])}", n, msg_arg(a, 2))
    return None


def a_bool(want):
    def fn(i, a, n):
        if a[0] is not want:
            fail(i, f"expected {str(want).lower()}, got {show(i, a[0])}", n, msg_arg(a, 1))
        return None
    return fn


def a_null(want_null):
    def fn(i, a, n):
        if (a[0] is None) != want_null:
            fail(i, f"expected null, got {show(i, a[0])}" if want_null else "expected a value, got null", n, msg_arg(a, 1))
        return None
    return fn


def contains(i, needle, hay, n):
    if isinstance(hay, str):
        if not isinstance(needle, str):
            i.throw("TypeException", f"Test.assertContains(): searching a string needs a string, got {type_name(needle)}", n)
        return needle in hay
    if isinstance(hay, (list, range)):
        return any(values_equal(needle, x) for x in hay)
    if isinstance(hay, BarqSet):
        return any(values_equal(needle, x) for x in hay.values())
    if isinstance(hay, dict):
        return any(values_equal(needle, k) for k in hay)
    i.throw("TypeException", f"Test.assertContains(): cannot search in {type_name(hay)}", n)


def a_contains(want):
    def fn(i, a, n):
        if contains(i, a[0], a[1], n) != want:
            fail(i, f"expected {show(i, a[1])} {'to contain' if want else 'not to contain'} {show(i, a[0])}", n, msg_arg(a, 2))
        return None
    return fn


def size_of(i, v, n, fname):
    if isinstance(v, (str, list, dict, range)):
        return len(v)
    if isinstance(v, BarqSet):
        return len(v.values())
    i.throw("TypeException", f"{fname}(): cannot count {type_name(v)}", n)


def a_count(i, a, n):
    want = need(i, a[0], ("int",), "Test.assertCount", 1, n)
    got = size_of(i, a[1], n, "Test.assertCount")
    if got != want:
        fail(i, f"expected {want} item{'s' if want != 1 else ''}, got {got}", n, msg_arg(a, 2))
    return None


def a_empty(want):
    def fn(i, a, n):
        empty = a[0] is None or size_of(i, a[0], n, "Test.assertEmpty") == 0
        if empty != want:
            fail(i, f"expected {'empty' if want else 'not empty'}, got {show(i, a[0])}", n, msg_arg(a, 1))
        return None
    return fn


def a_throws(i, a, n):
    cls = need(i, a[0], ("class",), "Test.assertThrows", 1, n)
    fn = a[1]
    if not is_callable(fn):
        i.throw("TypeException", f"Test.assertThrows() needs a function as argument 2, got {type_name(fn)}", n)
    part = arg(i, a, 2, ("string",), "Test.assertThrows", n, None)
    try:
        i.call_function(fn, [], n)
    except BarqThrow as t:
        obj = t.obj
        if not obj.cls.is_subclass_of(cls):
            fail(i, f"expected {cls.name}, got {obj.cls.name}: {to_display(obj.fields.get('message', ''), i)}", n)
        if part is not None and part not in to_display(obj.fields.get("message", ""), i):
            fail(i, f"expected the {cls.name} message to contain {show(i, part)}, got {show(i, obj.fields.get('message'))}", n)
        return obj
    fail(i, f"expected {cls.name} to be raised, but nothing was raised", n)


def a_instance(i, a, n):
    cls = need(i, a[0], ("class",), "Test.assertInstanceOf", 1, n)
    v = a[1]
    if not (isinstance(v, BarqObject) and v.cls.is_subclass_of(cls)):
        fail(i, f"expected an instance of {cls.name}, got {type_name(v)}", n, msg_arg(a, 2))
    return None


def a_matches(i, a, n):
    from .stdlib import compile_regex
    pattern = need(i, a[0], ("string",), "Test.assertMatches", 1, n)
    text = need(i, a[1], ("string",), "Test.assertMatches", 2, n)
    if compile_regex(i, pattern, n, "Test.assertMatches").search(text) is None:
        fail(i, f"expected {show(i, text)} to match {pattern}", n, msg_arg(a, 2))
    return None


def a_compare(op, word):
    def fn(i, a, n):
        x, y = need(i, a[0], ("number",), "Test." + word, 1, n), need(i, a[1], ("number",), "Test." + word, 2, n)
        ok = y > x if op == ">" else y < x
        if not ok:
            fail(i, f"expected a value {'greater' if op == '>' else 'less'} than {show(i, x)}, got {show(i, y)}", n, msg_arg(a, 2))
        return None
    return fn


def a_status(i, a, n):
    res = need(i, a[0], ("Response",), "Test.assertStatus", 1, n)
    want = need(i, a[1], ("int",), "Test.assertStatus", 2, n)
    got = res.fields["status"]
    if got != want:
        body = res.fields["body"]
        body = body if len(body) <= 200 else body[:200] + "…"
        fail(i, f"expected status {want}, got {got}" + (f" — body: {body}" if body else ""), n)
    return None


TEST = {
    "it": (add_test(False), 2, 2),
    "test": (add_test(False), 2, 2),
    "skip": (add_test(True), 1, 2),
    "describe": (test_describe, 2, 2),
    "beforeEach": (add_hook("beforeEach"), 1, 1),
    "afterEach": (add_hook("afterEach"), 1, 1),
    "run": (test_run, 0, 0),
    "client": (lambda i, a, n: new_test_client(i), 0, 0),
    "fail": (lambda i, a, n: fail(i, to_display(a[0], i) if a else "failed", n), 0, 1),
    "assertEquals": (a_equals, 2, 3),
    "assertNotEquals": (a_not_equals, 2, 3),
    "assertTrue": (a_bool(True), 1, 2),
    "assertFalse": (a_bool(False), 1, 2),
    "assertNull": (a_null(True), 1, 2),
    "assertNotNull": (a_null(False), 1, 2),
    "assertContains": (a_contains(True), 2, 3),
    "assertNotContains": (a_contains(False), 2, 3),
    "assertCount": (a_count, 2, 3),
    "assertEmpty": (a_empty(True), 1, 2),
    "assertNotEmpty": (a_empty(False), 1, 2),
    "assertThrows": (a_throws, 2, 3),
    "assertInstanceOf": (a_instance, 2, 3),
    "assertMatches": (a_matches, 2, 3),
    "assertGreaterThan": (a_compare(">", "assertGreaterThan"), 2, 3),
    "assertLessThan": (a_compare("<", "assertLessThan"), 2, 3),
    "assertStatus": (a_status, 2, 2),
}


# ===========================================================================
# install
# ===========================================================================

MODULES3 = {"Xml": XML, "Mail": MAIL, "Queue": QUEUE, "Cli": CLI, "Test": TEST}


def install(i):
    for name, parent in STDLIB3_EXCEPTIONS:
        cls = BarqClass(name, name, None, parent=i.classes[parent], native=True)
        i.classes[name] = cls
        i.builtins.declare(name, cls, None, const=True)
    native_class(i, "XmlNode", {"name": "string", "attrs": "dict", "text": "string", "children": "list"}, XML_NODE_METHODS)
    native_class(i, "Message", None, MESSAGE_METHODS)
    native_class(i, "PendingMail", None, PENDING_METHODS)
    native_class(i, "JobQueue", None, JOB_QUEUE_METHODS)
    for name, table in MODULES3.items():
        i.builtins.declare(name, module(name, table), None, const=True)
