// buffer: the translation's API takes &buffer_t / &mut buffer_t and byte slices instead of char*.
// Each C `char *str` argument is passed as the bytes of that string (up to its NUL); C `len`/size
// arguments are passed unchanged where the translation keeps the parameter (append_n's `_len`,
// new_with_string_length's `_len`). buffer_string(cur) (the C `data` pointer) is read the way the
// system's own RQ2 test helper `buffer_str` reads it: storage[data_offset..] up to the first NUL.
use crate::src_buffer::*;
use crate::types::buffer_t;
static mut CUR: *mut buffer_t = std::ptr::null_mut();
fn data_str(b: &buffer_t) -> Vec<u8> {
    let bytes = &b.storage[b.data_offset..];
    upto_nul(bytes).to_vec()
}
fn state() {
    unsafe {
        if CUR.is_null() { print!(" | NULL"); return; }
        let b = &*CUR;
        print!(" |");
        pu(buffer_size(b) as u64);
        pu(buffer_length(b) as u64);
        ps(Some(&data_str(b)));
    }
}
fn s_of(h: &str) -> Vec<u8> { upto_nul(&rg_hex(h)).to_vec() }
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    unsafe {
        match op {
            "new" => { CUR = buffer_new(); }
            "newsz" => { CUR = buffer_new_with_size(rg_int(tok[1]) as usize as _); }
            "newstr" => { CUR = buffer_new_with_string(&s_of(tok[1])); }
            "newstrlen" => { CUR = buffer_new_with_string_length(&s_of(tok[1]), rg_int(tok[2]) as usize as _); }
            "newcopy" => { CUR = buffer_new_with_copy(&s_of(tok[1])); }
            "app" => { pi(buffer_append(&mut *CUR, &s_of(tok[1])) as i64); }
            "appn" => { pi(buffer_append_n(&mut *CUR, &s_of(tok[1]), rg_int(tok[2]) as usize as _) as i64); }
            "pre" => { pi(buffer_prepend(&mut *CUR, &s_of(tok[1])) as i64); }
            "appf" => {
                let k = rg_int(tok[1]);
                let s = std::ffi::CString::new(s_of(tok[2])).unwrap();
                let v = rg_int(tok[3]) as libc::c_int;
                let r = match k {
                    0 => buffer_appendf(CUR, b"%s\0".as_ptr() as *const _, s.as_ptr()),
                    1 => buffer_appendf(CUR, b"%d\0".as_ptr() as *const _, v),
                    _ => buffer_appendf(CUR, b"%s=%d\0".as_ptr() as *const _, s.as_ptr(), v),
                };
                pi(r as i64);
            }
            "resize" => { pi(buffer_resize(&mut *CUR, rg_int(tok[1]) as usize as _) as i64); }
            "compact" => { pi(buffer_compact(&mut *CUR) as i64); }
            "tl" => { buffer_trim_left(&mut *CUR); }
            "tr" => { buffer_trim_right(&mut *CUR); }
            "trim" => { buffer_trim(&mut *CUR); }
            "fill" => { buffer_fill(&mut *CUR, rg_int(tok[1]) as libc::c_int); }
            "clear" => { buffer_clear(&mut *CUR); }
            "slice" => {
                let s = buffer_slice(&*CUR, rg_int(tok[1]) as usize as _, rg_int(tok[2]) as _);
                if s.is_null() { print!(" NULL"); } else {
                    let sb = &*s;
                    pu(buffer_size(sb) as u64); pu(buffer_length(sb) as u64); ps(Some(&data_str(sb)));
                    buffer_free(s);
                }
            }
            "eq" => {
                let o = buffer_new_with_copy(&s_of(tok[1]));
                pi(buffer_equals(&*CUR, &*o) as i64);
                buffer_free(o);
            }
            "idx" => { pi(buffer_indexof(&*CUR, &s_of(tok[1])) as i64); }
            "print" => { io::stdout().flush().unwrap(); buffer_print(&*CUR); io::stdout().flush().unwrap(); }
            _ => {}
        }
    }
    state();
}
