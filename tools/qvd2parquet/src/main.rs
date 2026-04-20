use std::env;
use std::fs::File;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Instant;

use arrow::array::{ArrayRef, Float64Builder, Int64Builder, StringBuilder};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;

use qvd::{open_qvd_stream, QvdSymbol, QvdValue};

const CHUNK_ROWS: usize = 65_536;

#[derive(Clone, Copy)]
enum ColType {
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

    let col_types: Vec<ColType> = (0..n_cols)
        .map(|i| infer_col_type(&reader.symbols[i]))
        .collect();

    let fields: Vec<Field> = reader
        .header
        .fields
        .iter()
        .enumerate()
        .map(|(i, f)| {
            let dt = match col_types[i] {
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
