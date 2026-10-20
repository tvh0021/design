//! Minimal protobuf wire-format reader. No schema: callers pick fields by
//! number. Only the four wire types in use (varint, 64-bit, length-delimited,
//! 32-bit) are understood; groups or malformed input end the message.

/// One decoded field value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Value<'a> {
    Varint(u64),
    Fixed64(u64),
    Bytes(&'a [u8]),
    Fixed32(u32),
}

/// Iterator over the top-level fields of one message.
#[derive(Debug, Clone)]
pub struct Fields<'a> {
    buf: &'a [u8],
    pos: usize,
    /// Set when decoding stopped on malformed input.
    pub malformed: bool,
}

pub fn fields(buf: &[u8]) -> Fields<'_> {
    Fields {
        buf,
        pos: 0,
        malformed: false,
    }
}

fn read_varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
    let mut v = 0u64;
    for shift in (0..64).step_by(7) {
        let b = *buf.get(*pos)?;
        *pos += 1;
        v |= u64::from(b & 0x7f) << shift;
        if b & 0x80 == 0 {
            return Some(v);
        }
    }
    None
}

impl<'a> Fields<'a> {
    fn next_field(&mut self) -> Option<(u32, Value<'a>)> {
        let key = read_varint(self.buf, &mut self.pos)?;
        let num = u32::try_from(key >> 3).ok().filter(|&n| n > 0)?;
        let val = match key & 7 {
            0 => Value::Varint(read_varint(self.buf, &mut self.pos)?),
            1 => {
                let b = self.buf.get(self.pos..self.pos + 8)?;
                self.pos += 8;
                Value::Fixed64(u64::from_le_bytes(b.try_into().ok()?))
            }
            2 => {
                let len = usize::try_from(read_varint(self.buf, &mut self.pos)?).ok()?;
                let end = self.pos.checked_add(len)?;
                let b = self.buf.get(self.pos..end)?;
                self.pos = end;
                Value::Bytes(b)
            }
            5 => {
                let b = self.buf.get(self.pos..self.pos + 4)?;
                self.pos += 4;
                Value::Fixed32(u32::from_le_bytes(b.try_into().ok()?))
            }
            _ => return None,
        };
        Some((num, val))
    }
}

impl<'a> Iterator for Fields<'a> {
    type Item = (u32, Value<'a>);

    fn next(&mut self) -> Option<Self::Item> {
        if self.malformed || self.pos >= self.buf.len() {
            return None;
        }
        let start = self.pos;
        let r = self.next_field();
        if r.is_none() {
            self.pos = start;
            self.malformed = true;
        }
        r
    }
}

/// True when `buf` decodes cleanly to the end as a message.
pub fn is_message(buf: &[u8]) -> bool {
    let mut f = fields(buf);
    f.by_ref().for_each(drop);
    !f.malformed
}

/// First varint with field number `num`.
pub fn varint(buf: &[u8], num: u32) -> Option<u64> {
    fields(buf).find_map(|(n, v)| match v {
        Value::Varint(x) if n == num => Some(x),
        _ => None,
    })
}

/// First length-delimited value with field number `num`.
pub fn bytes(buf: &[u8], num: u32) -> Option<&[u8]> {
    fields(buf).find_map(|(n, v)| match v {
        Value::Bytes(b) if n == num => Some(b),
        _ => None,
    })
}

/// Follow a path of nested length-delimited fields: `path(buf, &[1, 4])` is
/// field 4 inside field 1.
pub fn path<'a>(buf: &'a [u8], nums: &[u32]) -> Option<&'a [u8]> {
    nums.iter().try_fold(buf, |b, &n| bytes(b, n))
}

/// First field `num` as UTF-8 text.
pub fn string(buf: &[u8], num: u32) -> Option<&str> {
    std::str::from_utf8(bytes(buf, num)?).ok()
}

/// `google.protobuf.Timestamp`-shaped message (1: seconds, 2: nanos) to epoch ms.
pub fn timestamp_ms(buf: &[u8]) -> Option<i64> {
    let secs = i64::try_from(varint(buf, 1)?).ok()?;
    let nanos = varint(buf, 2).unwrap_or(0) as i64;
    Some(secs * 1000 + nanos / 1_000_000)
}

/// Depth-first walk over every length-delimited value, nested ones included.
/// `visit` returns `Some` to stop the walk with that result.
pub fn walk_bytes<'a, T>(
    buf: &'a [u8],
    depth: usize,
    visit: &mut impl FnMut(&'a [u8]) -> Option<T>,
) -> Option<T> {
    for (_, v) in fields(buf) {
        if let Value::Bytes(b) = v {
            if let Some(t) = visit(b) {
                return Some(t);
            }
            if depth > 0
                && b.len() > 1
                && is_message(b)
                && let Some(t) = walk_bytes(b, depth - 1, visit)
            {
                return Some(t);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // Hand-encoded:
    //   1: varint 150            -> 08 96 01
    //   2: bytes "hi"            -> 12 02 68 69
    //   3: message { 1: 7 }      -> 1a 02 08 07
    //   4: fixed32 1             -> 25 01 00 00 00
    //   5: fixed64 2             -> 29 02 00 00 00 00 00 00 00
    const MSG: &[u8] = &[
        0x08, 0x96, 0x01, 0x12, 0x02, b'h', b'i', 0x1a, 0x02, 0x08, 0x07, 0x25, 1, 0, 0, 0, 0x29,
        2, 0, 0, 0, 0, 0, 0, 0,
    ];

    #[test]
    fn decodes_each_wire_type() {
        let all: Vec<_> = fields(MSG).collect();
        assert_eq!(
            all,
            vec![
                (1, Value::Varint(150)),
                (2, Value::Bytes(b"hi")),
                (3, Value::Bytes(&[0x08, 0x07])),
                (4, Value::Fixed32(1)),
                (5, Value::Fixed64(2)),
            ]
        );
        assert!(is_message(MSG));
        assert_eq!(varint(MSG, 1), Some(150));
        assert_eq!(string(MSG, 2), Some("hi"));
        assert_eq!(path(MSG, &[3]).and_then(|m| varint(m, 1)), Some(7));
        assert_eq!(varint(MSG, 9), None);
    }

    #[test]
    fn malformed_input_stops_cleanly() {
        // length 5 but only 2 bytes follow
        let bad = [0x08, 0x01, 0x12, 0x05, b'a', b'b'];
        let mut f = fields(&bad);
        assert_eq!(f.next(), Some((1, Value::Varint(1))));
        assert_eq!(f.next(), None);
        assert!(f.malformed);
        assert!(!is_message(&bad));
        assert!(!is_message(b"plain text"));
        // field number 0 is invalid
        assert!(!is_message(&[0x00, 0x01]));
        // unterminated varint
        assert!(!is_message(&[0x08, 0xff]));
    }

    #[test]
    fn timestamp_to_ms() {
        // { 1: 1700000000, 2: 250000000 }
        let mut ts = vec![0x08];
        ts.extend([0x80, 0xe2, 0xcf, 0xaa, 0x06]);
        ts.extend([0x10, 0x80, 0xe5, 0x9a, 0x77]);
        assert_eq!(timestamp_ms(&ts), Some(1_700_000_000_250));
    }
}
