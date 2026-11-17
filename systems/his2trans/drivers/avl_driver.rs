// avl: the translation keeps repr(C) node/tree structs (bindgen) but takes &mut/& references; the
// tree root/first/last are *mut AVLBASE_NODE_S (same layout as the C AVL3_NODE*/AVL_NODE*).
// Same element pool and printing as drivers/avl_driver.c.
use crate::src_avl::*;
use crate::types::*;
use core::ffi::c_void;
#[repr(C)]
struct Elem { n3: AVL3_NODE, key: i32, n2: AVL_NODE }
static mut EP: *mut Elem = std::ptr::null_mut();
static mut T3: *mut AVL3_TREE = std::ptr::null_mut();
static mut T2: *mut AVL_TREE = std::ptr::null_mut();
static mut INFO: *mut AVL3_TREE_INFO = std::ptr::null_mut();
extern "C" fn cmp(a: *const c_void, b: *const c_void) -> core::ffi::c_int {
    let (x, y) = unsafe { (*(a as *const i32), *(b as *const i32)) };
    if x < y { -1 } else if x > y { 1 } else { 0 }
}
fn off3() -> usize { std::mem::offset_of!(Elem, n3) }
fn off2() -> usize { std::mem::offset_of!(Elem, n2) }
unsafe fn ixe(p: *const c_void) -> i64 {
    if p.is_null() { -1 } else { ((p as usize) - (EP as usize)) as i64 / std::mem::size_of::<Elem>() as i64 }
}
unsafe fn ix3(n: *const c_void) -> i64 { if n.is_null() { -1 } else { ixe((n as usize - off3()) as *const c_void) } }
unsafe fn ix2(n: *const c_void) -> i64 { if n.is_null() { -1 } else { ixe((n as usize - off2()) as *const c_void) } }
unsafe fn el(k: i64) -> &'static mut Elem { &mut *EP.add(k as usize) }
unsafe fn walk3(n: *mut AVL3_NODE) {
    if n.is_null() { return; }
    walk3((*n).pstLeft);
    let e = &*((n as usize - off3()) as *const Elem);
    print!(" ({} {} {} {} {} {} {})", ix3(n as _), e.key, (*n).sLHeight, (*n).sRHeight, ix3((*n).pstParent as _),
           ix3((*n).pstLeft as _), ix3((*n).pstRight as _));
    walk3((*n).pstRight);
}
unsafe fn walk2(n: *mut AVL_NODE) {
    if n.is_null() { return; }
    walk2((*n).pstLeft);
    print!(" ({} {} {} {} {} {} {} {})", ix2(n as _), *((*n).pKey as *const i32), (*n).sLHeight, (*n).sRHeight,
           ix2((*n).pstParent as _), ix2((*n).pstLeft as _), ix2((*n).pstRight as _), ixe((*n).pSelf as _));
    walk2((*n).pstRight);
}
unsafe fn init() {
    if !EP.is_null() { return; }
    let v: Vec<Elem> = (0..256).map(|_| core::mem::zeroed()).collect();
    EP = Box::leak(v.into_boxed_slice()).as_mut_ptr();
    T3 = Box::leak(Box::new(core::mem::zeroed::<AVL3_TREE>()));
    T2 = Box::leak(Box::new(core::mem::zeroed::<AVL_TREE>()));
    INFO = Box::leak(Box::new(core::mem::zeroed::<AVL3_TREE_INFO>()));
    VOS_AVL3_INIT_TREE(&mut *T3, &*INFO);
    VOS_AVL_INIT_TREE(&mut *T2, Some(cmp));
    (*INFO).pfCompare = Some(cmp);
    (*INFO).usKeyOffset = std::mem::offset_of!(Elem, key) as u16;
    (*INFO).usNodeOffset = off3() as u16;
}
fn dispatch(tok: &[&str]) {
    unsafe {
        init();
        let (t3, t2, info) = (&mut *T3, &mut *T2, &*INFO);
        match tok[0] {
            "i3" => {
                let e = el(rg_int(tok[1]));
                e.key = rg_int(tok[2]) as i32;
                VOS_AVL3_INIT_NODE(&mut e.n3);
                pi(ixe(VOS_AVL3_Insert_Or_Find(t3, &mut e.n3, info)));
            }
            "d3" => VOS_AVL3_Delete(t3, &mut el(rg_int(tok[1])).n3),
            "f3" => { let v = rg_int(tok[1]) as i32; pi(ixe(VOS_AVL3_Find(t3, &v as *const i32 as _, info))); }
            "fn3" => { let v = rg_int(tok[1]) as i32; pi(ixe(AVL3_Find_Or_Find_Next(t3, &v as *const i32 as _, rg_int(tok[2]) as u32, info))); }
            "first3" => pi(ixe(VOS_AVL3_First(t3, info))),
            "last3" => pi(ixe(VOS_AVL3_Last(t3, info))),
            "next3" => pi(ixe(VOS_AVL3_Next(&el(rg_int(tok[1])).n3, info))),
            "prev3" => pi(ixe(VOS_AVL3_Prev(&el(rg_int(tok[1])).n3, info))),
            "dump3" => {
                pi(ix3(t3.pstRoot as _)); pi(ix3(t3.pstFirst as _)); pi(ix3(t3.pstLast as _));
                walk3(t3.pstRoot as *mut AVL3_NODE);
            }
            "i2" => {
                let k = rg_int(tok[1]);
                let e = el(k);
                e.key = rg_int(tok[2]) as i32;
                let selfp = e as *mut Elem as *mut c_void;
                let keyp = &mut e.key as *mut i32 as *mut c_void;
                VOS_AVL_INIT_NODE(&mut e.n2, selfp, keyp);
                pi(ixe(VOS_AVL_Insert_Or_Find(t2, &mut e.n2)));
            }
            "ri2" => {
                pi(ixe(VOS_AVL_Insert_Or_Find(t2, &mut el(rg_int(tok[1])).n2)));
                pi(if t2.pstRoot.is_null() { -1 } else { ixe((*(t2.pstRoot as *mut AVL_NODE)).pSelf as _) });
            }
            "d2" => VOS_AVL_Delete(t2, &mut el(rg_int(tok[1])).n2),
            "f2" => { let v = rg_int(tok[1]) as i32; pi(ixe(VOS_AVL_Find(t2, &v as *const i32 as _))); }
            "fn2" => { let v = rg_int(tok[1]) as i32; pi(ixe(VOS_AVL_Find_Or_Find_Next(t2, &v as *const i32 as _, rg_int(tok[2]) as u32))); }
            "next2" => pi(ixe(VOS_AVL_Next(&el(rg_int(tok[1])).n2))),
            "prev2" => pi(ixe(VOS_AVL_Prev(&el(rg_int(tok[1])).n2))),
            "dump2" => {
                pi(ix2(t2.pstRoot as _)); pi(ix2(t2.pstFirst as _)); pi(ix2(t2.pstLast as _));
                walk2(t2.pstRoot as *mut AVL_NODE);
            }
            _ => {}
        }
    }
}
