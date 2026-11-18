//! `local_echo: true`: drop the echo of our own request that some 2-wire RS-485 adapters
//! return before the reply, the way pymodbus' `handle_local_echo` does: received bytes that
//! start with what we sent are dropped; a partial echo is dropped and the rest awaited; bytes
//! that are not the echo end the matching (the adapter does not echo after all).

use std::pin::Pin;
use std::task::{ready, Context, Poll};

use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

#[derive(Debug)]
pub struct LocalEcho<T> {
    inner: T,
    pending: Vec<u8>,
}

impl<T> LocalEcho<T> {
    pub fn new(inner: T) -> Self {
        Self { inner, pending: Vec::new() }
    }
}

impl<T: AsyncRead + Unpin> AsyncRead for LocalEcho<T> {
    fn poll_read(mut self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<std::io::Result<()>> {
        let this = &mut *self;
        loop {
            let before = buf.filled().len();
            ready!(Pin::new(&mut this.inner).poll_read(cx, buf))?;
            let after = buf.filled().len();
            if after == before || this.pending.is_empty() {
                return Poll::Ready(Ok(())); // end of stream, or nothing to strip
            }
            let data = &buf.filled()[before..after];
            let strip = if data.starts_with(&this.pending) {
                std::mem::take(&mut this.pending).len()
            } else if this.pending.starts_with(data) {
                this.pending.drain(..data.len());
                data.len()
            } else {
                this.pending.clear();
                0
            };
            if strip > 0 {
                buf.filled_mut().copy_within(before + strip..after, before);
                buf.set_filled(after - strip);
            }
            if buf.filled().len() > before {
                return Poll::Ready(Ok(()));
            }
            // everything was echo: read on (Ready with no bytes would mean end of stream)
        }
    }
}

impl<T: AsyncWrite + Unpin> AsyncWrite for LocalEcho<T> {
    fn poll_write(mut self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<std::io::Result<usize>> {
        let n = ready!(Pin::new(&mut self.inner).poll_write(cx, buf))?;
        self.pending.extend_from_slice(&buf[..n]);
        Poll::Ready(Ok(n))
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn the_echo_is_dropped_whole_or_in_pieces() {
        let (ours, mut adapter) = tokio::io::duplex(256);
        let mut line = LocalEcho::new(ours);
        line.write_all(b"\x01\x03\x00\x00\x00\x02\xc4\x0b").await.unwrap();
        let mut req = [0u8; 8];
        adapter.read_exact(&mut req).await.unwrap();
        // the adapter echoes in two pieces, then the device replies
        adapter.write_all(&req[..3]).await.unwrap();
        adapter.flush().await.unwrap();
        tokio::task::yield_now().await;
        adapter.write_all(&req[3..]).await.unwrap();
        adapter.write_all(b"REPLY").await.unwrap();
        let mut got = [0u8; 5];
        line.read_exact(&mut got).await.unwrap();
        assert_eq!(&got, b"REPLY");
    }

    #[tokio::test]
    async fn without_an_echo_the_reply_is_kept() {
        let (ours, mut adapter) = tokio::io::duplex(256);
        let mut line = LocalEcho::new(ours);
        line.write_all(b"\x01\x03\x00\x00").await.unwrap();
        let mut req = [0u8; 4];
        adapter.read_exact(&mut req).await.unwrap();
        adapter.write_all(b"\x01\x83\x02").await.unwrap(); // not our bytes: a reply
        let mut got = [0u8; 3];
        line.read_exact(&mut got).await.unwrap();
        assert_eq!(&got, b"\x01\x83\x02");
        // matching has ended for this request: a later copy of the request is data
        adapter.write_all(b"\x01\x03").await.unwrap();
        let mut more = [0u8; 2];
        line.read_exact(&mut more).await.unwrap();
        assert_eq!(&more, b"\x01\x03");
    }
}
