const path = require('path');
const { round } = require('./pricing');

exports.fileFor = function (sku) {
  return path.join('carts', `${sku}.json`);
};

module.exports.cost = (cents) => round(cents);
