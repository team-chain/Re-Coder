import Stripe from 'stripe';
import dotenv from 'dotenv';

dotenv.config();

const STRIPE_SECRET_KEY = process.env.STRIPE_SECRET_KEY;
const STRIPE_WEBHOOK_SECRET = process.env.STRIPE_WEBHOOK_SECRET;
const NODE_ENV = process.env.NODE_ENV;
const PAYMENT_MODE = process.env.PAYMENT_MODE;
const STRIPE_MOCK_HOST = process.env.STRIPE_MOCK_HOST;
const STRIPE_MOCK_PORT = process.env.STRIPE_MOCK_PORT;

// Validate required environment variables
if (!STRIPE_SECRET_KEY) {
  throw new Error('STRIPE_SECRET_KEY environment variable is required');
}

if (!STRIPE_WEBHOOK_SECRET) {
  throw new Error('STRIPE_WEBHOOK_SECRET environment variable is required');
}

// Validate mock configuration
if (PAYMENT_MODE === 'mock') {
  if (NODE_ENV !== 'test') {
    throw new Error('PAYMENT_MODE=mock is only allowed when NODE_ENV=test');
  }
  if (!STRIPE_MOCK_HOST || !STRIPE_MOCK_PORT) {
    throw new Error('STRIPE_MOCK_HOST and STRIPE_MOCK_PORT are required when PAYMENT_MODE=mock');
  }
}

// Reject mock configuration in production
if (NODE_ENV === 'production' && (STRIPE_MOCK_HOST || STRIPE_MOCK_PORT)) {
  throw new Error('Mock Stripe configuration is not allowed in production environment');
}

// Create Stripe client configuration
const stripeConfig = {
  timeout: 5000,
  maxNetworkRetries: 0
};

// Add mock host configuration if in test mode with mock payment
if (NODE_ENV === 'test' && PAYMENT_MODE === 'mock') {
  stripeConfig.host = STRIPE_MOCK_HOST;
  stripeConfig.port = parseInt(STRIPE_MOCK_PORT, 10);
  stripeConfig.protocol = 'http';
}

// Create and export Stripe singleton instance
const stripe = new Stripe(STRIPE_SECRET_KEY, stripeConfig);

export default stripe;
export { STRIPE_WEBHOOK_SECRET };
