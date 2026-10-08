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
        name: 'Wireless Headphones',
        description: 'High-quality wireless headphones with noise cancellation',
        price: 99.99,
        stock: 50,
        image_url: 'https://placehold.co/300x200?text=Wireless+Headphones'
      },
      {
        name: 'USB-C Cable',
        description: 'Durable USB-C charging and data cable',
        price: 19.99,
        stock: 200,
        image_url: 'https://placehold.co/300x200?text=USB-C+Cable'
      },
      {
        name: 'Portable Charger',
        description: '20000mAh portable power bank with fast charging',
        price: 49.99,
        stock: 75,
        image_url: 'https://placehold.co/300x200?text=Portable+Charger'
      },
      {
        name: 'Mechanical Keyboard',
        description: 'RGB mechanical keyboard with Cherry MX switches',
        price: 129.99,
        stock: 30,
        image_url: 'https://placehold.co/300x200?text=Mechanical+Keyboard'
      },
      {
        name: 'Wireless Mouse',
        description: 'Ergonomic wireless mouse with precision tracking',
        price: 39.99,
        stock: 100,
        image_url: 'https://placehold.co/300x200?text=Wireless+Mouse'
      },
      {
        name: 'Monitor Stand',
        description: 'Adjustable monitor stand with storage',
        price: 59.99,
        stock: 40,
        image_url: 'https://placehold.co/300x200?text=Monitor+Stand'
      },
      {
        name: 'Webcam 1080p',
        description: 'Full HD webcam with auto-focus and built-in microphone',
        price: 79.99,
        stock: 60,
        image_url: 'https://placehold.co/300x200?text=Webcam+1080p'
      },
      {
        name: 'USB Hub',
        description: '7-port USB 3.0 hub with power adapter',
        price: 34.99,
        stock: 80,
        image_url: 'https://placehold.co/300x200?text=USB+Hub'
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
