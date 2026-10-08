import pkg from 'pg';
const { Pool } = pkg;
import dotenv from 'dotenv';

dotenv.config();

// Parse NUMERIC/DECIMAL as float
pkg.types.setTypeParser(1700, (value) => parseFloat(value));

const sslConfig = process.env.DB_SSL === 'true'
  ? { rejectUnauthorized: true }
  : false;

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: sslConfig,
  connectionTimeoutMillis: 5000,
  query_timeout: 5000
});

pool.on('error', (err) => {
  console.error('Unexpected error on idle client', err);
});

export default pool;
