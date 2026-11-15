import { extractAwemeId } from '../../src/videos/video-url';

describe('extractAwemeId', () => {
  it.each([
    ['https://www.douyin.com/video/7481199234537033018', '7481199234537033018'],
    ['https://douyin.com/video/7481199234537033018/', '7481199234537033018'],
    ['https://www.douyin.com/video/7481199234537033018?previous_page=app_code_link', '7481199234537033018'],
    ['https://www.iesdouyin.com/share/video/7481199234537033018/', '7481199234537033018'],
  ])('reads the id from the path of %s', (url, id) => {
    expect(extractAwemeId(url)).toBe(id);
  });

  it.each([
    ['https://www.douyin.com/?modal_id=7481199234537033018', '7481199234537033018'],
    ['https://www.douyin.com/jingxuan?modal_id=7481199234537033018&x=1', '7481199234537033018'],
    ['https://www.douyin.com/user/MS4wLjABAAAA?modal_id=7481199234537033018', '7481199234537033018'],
  ])('reads the id from modal_id on %s', (url, id) => {
    expect(extractAwemeId(url)).toBe(id);
  });

  it.each([
    ['a profile page', 'https://www.douyin.com/user/MS4wLjABAAAA'],
    ['the home page', 'https://www.douyin.com/'],
    ['a short link, which needs a redirect to resolve', 'https://v.douyin.com/iAbCdEf/'],
    ['a non-numeric id', 'https://www.douyin.com/video/abc'],
    ['a non-numeric modal_id', 'https://www.douyin.com/?modal_id=abc'],
    ['an image-post path', 'https://www.douyin.com/note/7481199234537033018'],
    ['not a URL at all', 'not a url'],
  ])('returns null for %s', (_label, url) => {
    expect(extractAwemeId(url)).toBeNull();
  });
});
