export async function retry(task, attempts) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await task();
    } catch (err) {
      if (i === attempts - 1) throw err;
    }
  }
}
