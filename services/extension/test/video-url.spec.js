'use strict';

const { isVideoPageUrl } = require('../lib/video-url');

describe('isVideoPageUrl', () => {
  test.each([
    'https://www.douyin.com/video/7481199234537033018',
    'https://www.douyin.com/video/7481199234537033018?previous_page=app_code_link',
    'https://www.douyin.com/jingxuan?modal_id=7481199234537033018',
    'https://www.douyin.com/user/MS4wLjABAAAA?modal_id=7481199234537033018&showTab=post',
  ])('is true for %s', (url) => {
    expect(isVideoPageUrl(url)).toBe(true);
  });

  test.each([
    'https://www.douyin.com/',
    'https://www.douyin.com/user/MS4wLjABAAAA',
    'https://www.douyin.com/video/abc',
    'https://www.douyin.com/?modal_id=abc',
    'https://www.douyin.com/note/7481199234537033018',
    'not a url',
    '',
  ])('is false for %s', (url) => {
    expect(isVideoPageUrl(url)).toBe(false);
  });
});
