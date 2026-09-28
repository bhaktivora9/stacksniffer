import { Router } from 'express';

export class OrderService {
  /** @param {Map} db */
  constructor(db) {
    this.db = db;
  }

  find(id) {
    return this.db.get(id) ?? null;
  }
}

export function ordersRouter(store) {
  const router = Router();
  /** @type {OrderService} */
  const service = new OrderService(store);
  router.get('/:id', (req, res) => {
    const order = service.find(req.params.id);
    res.status(order ? 200 : 404).json(order);
  });
  return router;
}
