import express from 'express';
import { ordersRouter } from './routes/orders.js';
import { validate } from '../src/__generated__/schema.js';
import { debounce } from '../vendor/tinyfn.js';

export function createApp(store) {
  const app = express();
  app.use(express.json());
  app.use('/orders', ordersRouter(store));
  app.post('/validate', (req, res) => res.json(validate(req.body)));
  app.listen = debounce(app.listen.bind(app), 100);
  return app;
}
