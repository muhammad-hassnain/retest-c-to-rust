// urlparser: the translation's API is Rust-native: &str inputs, Option<String> results,
// url_parse(&str) -> Option<UrlData> with String fields. A C NULL return maps to None.
// The generator emits ASCII-only URLs (valid UTF-8, so every C input is representable as &str).
use crate::src_url::*;
fn o(s: Option<String>) { match s { None => print!(" NULL"), Some(v) => psn(v.as_bytes()) } }
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    let b = rg_hex(tok[1]);
    let u = std::str::from_utf8(upto_nul(&b)).unwrap();
    match op {
        "isp" => pi(url_is_protocol(u) as i64),
        "isssh" => pi(url_is_ssh(u) as i64),
        "gp" => o(url_get_protocol(u)),
        "ga" => o(url_get_auth(u)),
        "ghn" => o(url_get_hostname(u)),
        "gh" => o(url_get_host(u)),
        "gpath" => o(url_get_path(u)),
        "gpn" => o(url_get_pathname(u)),
        "gs" => o(url_get_search(u)),
        "gq" => o(url_get_query(u)),
        "gha" => o(url_get_hash(u)),
        "gport" => o(url_get_port(u)),
        "parse" => match url_parse(u) {
            None => print!(" NULL"),
            Some(d) => {
                for f in [&d.href, &d.protocol, &d.host, &d.auth, &d.hostname, &d.pathname, &d.search,
                          &d.path, &d.hash, &d.query, &d.port] { psn(f.as_bytes()); }
            }
        },
        "insp" => { print!(" "); url_inspect(u); }
        _ => {}
    }
}
