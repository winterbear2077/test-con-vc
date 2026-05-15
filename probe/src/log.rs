use std::io::Write;

pub fn log(f: &mut dyn Write, msg: &str) {
    let _ = writeln!(f, "{}", msg);
    let _ = f.flush();
}
