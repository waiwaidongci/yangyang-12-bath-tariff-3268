const pages = new Set(['observations','analysis','result','reports','billing']);
export const route = () => {
  const [page,id] = location.hash.slice(1).split('/');
  return {page:pages.has(page) ? page : 'observations',id};
};
