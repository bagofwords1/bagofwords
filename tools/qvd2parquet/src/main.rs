use std::env;
use std::fs::File;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Instant;

use arrow::array::{
    ArrayRef, Date32Builder, Float64Builder, Int64Builder, StringBuilder,
    Time64MicrosecondBuilder, TimestampMicrosecondBuilder,
};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use arrow::record_batch::RecordBatch;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;

use qvd::{open_qvd_stream, QvdSymbol, QvdValue};

const CHUNK_ROWS: usize = 65_536;

// QlikView (like Excel) counts days from 1899-12-30. Arrow's Date32/Timestamp
// count from the Unix epoch (1970-01-01). The two epochs are exactly 25569 days
// apart. Qlik uses the correct 1899-12-30 origin and does NOT inherit Excel's
// fictional-1900-leap-year bug, so this flat offset is right for every Qlik date.
const QLIK_EPOCH_OFFSET_DAYS: f64 = 25_569.0;
const SECS_PER_DAY: f64 = 86_400.0;
const MICROS_PER_SEC: f64 = 1_000_000.0;
const MICROS_PER_DAY: i64 = 86_400 * 1_000_000;

#[derive(Clone, Copy)]
enum ColType {
    Date,
    Timestamp,
    Time,
    Int,
    Double,
    Text,
}

fn infer_col_type(symbols: &[QvdSymbol]) -> ColType {
    if symbols.is_empty() {
        return ColType::Text;
    }
    let mut all_int = true;
    let mut all_numeric = true;
    for sym in symbols {
        match sym {
            QvdSymbol::Int(_) | QvdSymbol::DualInt(_, _) => {}
            QvdSymbol::Double(_) | QvdSymbol::DualDouble(_, _) => all_int = false,
            QvdSymbol::Text(_) => {
                all_numeric = false;
                break;
            }
        }
    }
    if !all_numeric {
        ColType::Text
    } else if all_int {
        ColType::Int
    } else {
        ColType::Double
    }
}

/// Decide a column's output type. Qlik dates/times are "dual" values whose
/// numeric leg is a serial number (days, or fraction of a day for time-of-day)
/// — `infer_col_type` alone would bucket them as Int/Double and emit the raw
/// serial. So we first consult the header's semantic hints (`tags` such as
/// `$timestamp`/`$date`/`$time`, then the NumberFormat `Type`) and only treat a
/// column as temporal when its symbol table is actually all-numeric. Anything
/// else falls back to the symbol-based inference.
fn classify(tags: &[String], format_type: &str, symbols: &[QvdSymbol]) -> ColType {
    let numeric = !symbols.is_empty()
        && symbols.iter().all(|s| !matches!(s, QvdSymbol::Text(_)));
    if numeric {
        let has_tag = |t: &str| tags.iter().any(|x| x == t);
        // Precedence: timestamp (date+time) → time-of-day → date.
        if has_tag("$timestamp") || format_type.eq_ignore_ascii_case("TIMESTAMP") {
            return ColType::Timestamp;
        }
        if has_tag("$time") || format_type.eq_ignore_ascii_case("TIME") {
            return ColType::Time;
        }
        if has_tag("$date") || format_type.eq_ignore_ascii_case("DATE") {
            return ColType::Date;
        }
    }
    infer_col_type(symbols)
}

/// Qlik serial (days since 1899-12-30) → Arrow Date32 (days since 1970-01-01).
fn serial_to_date32(serial: f64) -> i32 {
    (serial - QLIK_EPOCH_OFFSET_DAYS).round() as i32
}

/// Qlik serial → Arrow Timestamp microseconds (since 1970-01-01, no timezone).
fn serial_to_micros(serial: f64) -> i64 {
    ((serial - QLIK_EPOCH_OFFSET_DAYS) * SECS_PER_DAY * MICROS_PER_SEC).round() as i64
}

/// Qlik time-of-day (fraction of a day) → Arrow Time64 microseconds since
/// midnight. Keep only the fractional part so a stray date component can't push
/// the value out of the valid [0, 86_400_000_000) range.
fn serial_to_time_micros(serial: f64) -> i64 {
    let frac = serial - serial.floor();
    let micros = (frac * SECS_PER_DAY * MICROS_PER_SEC).round() as i64;
    micros.clamp(0, MICROS_PER_DAY - 1)
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: {} <input.qvd> <output.parquet>", args[0]);
        return ExitCode::from(2);
    }
    let input = &args[1];
    let output = &args[2];

    match run(input, output) {
        Ok(rows) => {
            eprintln!("qvd2parquet: wrote {} rows to {}", rows, output);
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("qvd2parquet: error: {}", e);
            ExitCode::from(1)
        }
    }
}

fn run(input: &str, output: &str) -> Result<usize, Box<dyn std::error::Error>> {
    let t0 = Instant::now();
    let mut reader = open_qvd_stream(input)?;
    let n_cols = reader.header.fields.len();

    let col_types: Vec<ColType> = reader
        .header
        .fields
        .iter()
        .enumerate()
        .map(|(i, f)| classify(&f.tags, &f.number_format.format_type, &reader.symbols[i]))
        .collect();

    let fields: Vec<Field> = reader
        .header
        .fields
        .iter()
        .enumerate()
        .map(|(i, f)| {
            let dt = match col_types[i] {
                ColType::Date => DataType::Date32,
                ColType::Timestamp => DataType::Timestamp(TimeUnit::Microsecond, None),
                ColType::Time => DataType::Time64(TimeUnit::Microsecond),
                ColType::Int => DataType::Int64,
                ColType::Double => DataType::Float64,
                ColType::Text => DataType::Utf8,
            };
            Field::new(&f.field_name, dt, true)
        })
        .collect();
    let schema = Arc::new(Schema::new(fields));

    let file = File::create(output)?;
    let props = WriterProperties::builder()
        .set_compression(Compression::SNAPPY)
        .build();
    let mut writer = ArrowWriter::try_new(file, schema.clone(), Some(props))?;

    let mut total: usize = 0;
    while let Some(chunk) = reader.next_chunk(CHUNK_ROWS)? {
        let n_rows = chunk.num_rows;
        total += n_rows;

        let mut arrays: Vec<ArrayRef> = Vec::with_capacity(n_cols);
        for (col_idx, ct) in col_types.iter().enumerate() {
            let col = &chunk.columns[col_idx];
            let arr: ArrayRef = match ct {
                ColType::Date => {
                    let mut b = Date32Builder::with_capacity(n_rows);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(s) => match s.as_f64() {
                                Some(f) if f.is_finite() => b.append_value(serial_to_date32(f)),
                                _ => b.append_null(),
                            },
                        }
                    }
                    Arc::new(b.finish())
                }
                ColType::Timestamp => {
                    let mut b = TimestampMicrosecondBuilder::with_capacity(n_rows);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(s) => match s.as_f64() {
                                Some(f) if f.is_finite() => b.append_value(serial_to_micros(f)),
                                _ => b.append_null(),
                            },
                        }
                    }
                    Arc::new(b.finish())
                }
                ColType::Time => {
                    let mut b = Time64MicrosecondBuilder::with_capacity(n_rows);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(s) => match s.as_f64() {
                                Some(f) if f.is_finite() => b.append_value(serial_to_time_micros(f)),
                                _ => b.append_null(),
                            },
                        }
                    }
                    Arc::new(b.finish())
                }
                ColType::Int => {
                    let mut b = Int64Builder::with_capacity(n_rows);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(QvdSymbol::Int(i)) => b.append_value(*i as i64),
                            QvdValue::Symbol(QvdSymbol::DualInt(i, _)) => b.append_value(*i as i64),
                            QvdValue::Symbol(_) => b.append_null(),
                        }
                    }
                    Arc::new(b.finish())
                }
                ColType::Double => {
                    let mut b = Float64Builder::with_capacity(n_rows);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(s) => match s.as_f64() {
                                Some(f) => b.append_value(f),
                                None => b.append_null(),
                            },
                        }
                    }
                    Arc::new(b.finish())
                }
                ColType::Text => {
                    let mut b = StringBuilder::with_capacity(n_rows, n_rows * 16);
                    for v in col {
                        match v {
                            QvdValue::Null => b.append_null(),
                            QvdValue::Symbol(s) => b.append_value(s.to_string_repr()),
                        }
                    }
                    Arc::new(b.finish())
                }
            };
            arrays.push(arr);
        }

        let batch = RecordBatch::try_new(schema.clone(), arrays)?;
        writer.write(&batch)?;
    }
    writer.close()?;

    eprintln!(
        "qvd2parquet: {} rows, {} cols, {:.2}s",
        total,
        n_cols,
        t0.elapsed().as_secs_f64()
    );
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ct(c: ColType) -> &'static str {
        match c {
            ColType::Date => "date",
            ColType::Timestamp => "timestamp",
            ColType::Time => "time",
            ColType::Int => "int",
            ColType::Double => "double",
            ColType::Text => "text",
        }
    }

    #[test]
    fn date_epoch_anchors() {
        // Serial 25569 is the Unix epoch (1970-01-01) → Date32 day 0.
        assert_eq!(serial_to_date32(25_569.0), 0);
        assert_eq!(serial_to_date32(25_570.0), 1);
        assert_eq!(serial_to_date32(25_568.0), -1);
        // 2019-01-01 is 17897 days after the Unix epoch; Qlik serial 43466.
        assert_eq!(serial_to_date32(43_466.0), 17_897);
    }

    #[test]
    fn timestamp_epoch_and_fraction() {
        assert_eq!(serial_to_micros(25_569.0), 0);
        // Half a day past the epoch == 12:00:00 == 43_200s.
        assert_eq!(serial_to_micros(25_569.5), 43_200 * 1_000_000);
    }

    #[test]
    fn time_of_day_keeps_only_fraction() {
        assert_eq!(serial_to_time_micros(0.0), 0);
        assert_eq!(serial_to_time_micros(0.5), 43_200 * 1_000_000); // 12:00:00
        // A timestamp-shaped serial (date + .75) yields only the 18:00 part.
        assert_eq!(serial_to_time_micros(43_831.75), 64_800 * 1_000_000);
        // Stays strictly inside [0, one day).
        assert!(serial_to_time_micros(0.999_999_999) < MICROS_PER_DAY);
    }

    #[test]
    fn classify_uses_tags_for_temporal() {
        let numeric = [QvdSymbol::Int(1), QvdSymbol::Int(2)];
        assert_eq!(
            ct(classify(&["$numeric".into(), "$timestamp".into()], "UNKNOWN", &numeric)),
            "timestamp"
        );
        assert_eq!(ct(classify(&["$date".into()], "UNKNOWN", &numeric)), "date");
        assert_eq!(ct(classify(&["$time".into()], "UNKNOWN", &numeric)), "time");
    }

    #[test]
    fn classify_falls_back_to_number_format() {
        let numeric = [QvdSymbol::Double(1.0)];
        // No semantic tags, but NumberFormat.Type names the type.
        assert_eq!(ct(classify(&[], "DATE", &numeric)), "date");
        assert_eq!(ct(classify(&[], "TIMESTAMP", &numeric)), "timestamp");
        // REAL/INTEGER are not temporal → plain numeric inference.
        assert_eq!(ct(classify(&[], "REAL", &numeric)), "double");
    }

    #[test]
    fn classify_guards_against_text_columns() {
        // A column tagged as a date but actually holding text must not be
        // coerced to a temporal type (which would null out every value).
        let textual = [QvdSymbol::Int(1), QvdSymbol::Text("not a date".into())];
        assert_eq!(ct(classify(&["$date".into()], "DATE", &textual)), "text");
        // An empty symbol table defaults to text rather than a bogus date.
        assert_eq!(ct(classify(&["$timestamp".into()], "TIMESTAMP", &[])), "text");
    }
}
