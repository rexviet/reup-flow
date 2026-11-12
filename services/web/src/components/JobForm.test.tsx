import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { JobForm } from './JobForm';

describe('JobForm', () => {
  it('calls onSubmit with the trimmed URL and clears the field on success', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<JobForm onSubmit={onSubmit} />);

    const input = screen.getByLabelText(/url profile/i);
    await userEvent.type(input, '  https://www.douyin.com/user/xyz  ');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));

    expect(onSubmit).toHaveBeenCalledWith('https://www.douyin.com/user/xyz', { autoLocalize: false, localizeNow: false });
    expect(input).toHaveValue('');
  });

  it('does not submit an empty URL', async () => {
    const onSubmit = vi.fn();
    render(<JobForm onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('shows the error message returned by onSubmit and keeps the input value', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('url must be an http(s) URL on one of: douyin.com'));
    render(<JobForm onSubmit={onSubmit} />);

    const input = screen.getByLabelText(/url profile/i);
    await userEvent.type(input, 'not-a-url');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));

    expect(await screen.findByText(/url must be an http\(s\) URL/)).toBeInTheDocument();
    expect(input).toHaveValue('not-a-url');
  });

  it('disables the submit button while a submission is in flight', async () => {
    let resolveSubmit: () => void = () => {};
    const onSubmit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveSubmit = resolve;
        }),
    );
    render(<JobForm onSubmit={onSubmit} />);

    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/user/xyz');
    const button = screen.getByRole('button', { name: /download all/i });
    await userEvent.click(button);

    expect(button).toBeDisabled();
    await act(async () => {
      resolveSubmit();
    });
  });

  it('passes the auto-localize switch and offers Localize ngay only for a single-video URL', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<JobForm onSubmit={onSubmit} />);

    const input = screen.getByLabelText(/url profile/i);
    await userEvent.type(input, 'https://www.douyin.com/user/xyz');
    expect(screen.queryByLabelText(/localize ngay/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/tự localize/i)).not.toBeChecked();

    await userEvent.click(screen.getByLabelText(/tự localize/i));
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    expect(onSubmit).toHaveBeenLastCalledWith('https://www.douyin.com/user/xyz', { autoLocalize: true, localizeNow: false });

    await userEvent.type(input, 'https://www.douyin.com/video/7300000000000000001');
    expect(screen.queryByLabelText(/tự localize/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/localize ngay/i));
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    expect(onSubmit).toHaveBeenLastCalledWith('https://www.douyin.com/video/7300000000000000001', {
      autoLocalize: true,
      localizeNow: true,
    });
  });
});
