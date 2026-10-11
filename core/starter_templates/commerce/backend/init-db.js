import pool from './src/db.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';

dotenv.config();

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function initializeDatabase() {
  const client = await pool.connect();

  try {
    console.log('Starting database initialization...');

    // Read schema file
    const schemaPath = path.join(__dirname, 'src', 'schema.sql');
    const schema = fs.readFileSync(schemaPath, 'utf-8');

    // Execute schema
    await client.query(schema);

    console.log('Database schema created successfully');

    // Insert sample data
    console.log('Inserting sample data...');

    // Sample products
    const products = [
      {
        name: '무선 헤드폰',
        description: '노이즈 캔슬링을 지원하는 고음질 무선 헤드폰',
        price: 129000,
        stock: 50,
        image_url: '/products/headphones.svg'
      },
      {
        name: 'USB-C 케이블',
        description: '충전과 데이터 전송을 지원하는 튼튼한 USB-C 케이블',
        price: 12900,
        stock: 200,
        image_url: '/products/usb-c-cable.svg'
      },
      {
        name: '보조배터리',
        description: '고속 충전을 지원하는 20000mAh 보조배터리',
        price: 39000,
        stock: 75,
        image_url: '/products/power-bank.svg'
      },
      {
        name: '기계식 키보드',
        description: '체리 MX 스위치를 쓴 RGB 기계식 키보드',
        price: 159000,
        stock: 30,
        image_url: '/products/keyboard.svg'
      },
      {
        name: '무선 마우스',
        description: '정밀 트래킹을 지원하는 인체공학 무선 마우스',
        price: 45000,
        stock: 100,
        image_url: '/products/mouse.svg'
      },
      {
        name: '모니터 받침대',
        description: '높이 조절과 수납이 되는 모니터 받침대',
        price: 59000,
        stock: 40,
        image_url: '/products/monitor-stand.svg'
      },
      {
        name: '1080p 웹캠',
        description: '자동 초점과 마이크를 갖춘 풀HD 웹캠',
        price: 89000,
        stock: 60,
        image_url: '/products/webcam.svg'
      },
      {
        name: 'USB 허브',
        description: '전원 어댑터가 포함된 7포트 USB 3.0 허브',
        price: 32000,
        stock: 80,
        image_url: '/products/usb-hub.svg'
      }
    ];

    const existing = await client.query('SELECT COUNT(*) AS count FROM products');
    for (const product of (process.env.SEED_DEMO === 'true' && Number(existing.rows[0].count) === 0 ? products : [])) {
      await client.query(
        `INSERT INTO products (name, description, price, stock, image_url)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT DO NOTHING`,
        [product.name, product.description, product.price, product.stock, product.image_url]
      );
    }

    console.log('Sample products inserted');

    // Verify data
    const productCount = await client.query('SELECT COUNT(*) FROM products');
    const userCount = await client.query('SELECT COUNT(*) FROM users');

    console.log(`\nDatabase initialization complete!`);
    console.log(`Products: ${productCount.rows[0].count}`);
    console.log(`Users: ${userCount.rows[0].count}`);
    console.log('\nNext steps:');
    console.log('1. Create admin user via API: POST /api/auth/register');
    console.log('2. Update user role to admin in database');
    console.log('3. Start server: npm start');
  } catch (err) {
    console.error('Database initialization error:', err);
    process.exit(1);
  } finally {
    client.release();
    await pool.end();
  }
}

initializeDatabase();
