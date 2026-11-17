// ht: hash_key is private in src_ht; run.py appends a one-line pub wrapper
// `pub fn regrade_hash_key(k: &core::ffi::CStr) -> u64 { hash_key(k) }` to the scratch copy.
fn dispatch(tok: &[&str]) {
    if tok[0] == "hash" {
        let b = rg_hex(tok[1]);
        let c = std::ffi::CString::new(upto_nul(&b).to_vec()).unwrap();
        pu(crate::src_ht::regrade_hash_key(c.as_c_str()));
    }
}
